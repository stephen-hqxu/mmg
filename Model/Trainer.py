from Model.Component.Coder import CoderMask
from Model.Component.Transformer import Transformer as Gen
from Model.Component.Discriminator import Discriminator as Disc

from Model.Setting import EmbeddingSetting, DatasetSetting, TrainingSetting

from Model import DataUtility

import torch
from torch import Tensor
from torch.nn import BCELoss
from torch.utils.data import DataLoader
from torch.utils.tensorboard.writer import SummaryWriter
from torch.optim import Adam

import datetime
from enum import IntEnum
from typing import Tuple

import os

class Trainer():
    """
    Matching-based MIDI humanisation model training.

    @see https://github.com/soumith/ganhacks regarding choice of model architecture and hyperparameter.
    """
    FAKE_LABEL: float = 0.0
    REAL_LABEL: float = 1.0

    class OperationMode(IntEnum):
        """
        @brief The mode of operation.
        """
        INFERENCE = 0x00
        TRAIN = 0xFF

    def __init__(this, log_name: str):
        """
        @brief Create a trainer with untrained model with random initial state.

        @param log_name The directory name to store training stats for the current session.
        """
        this.LogName: str = log_name
        this.Summary: SummaryWriter = SummaryWriter(DatasetSetting.TRAIN_STATS_LOG_PATH + '/' + this.LogName)

        this.Generator: Gen = Gen()
        this.Discriminator: Disc = Disc()

        # FIXME: definitely should use dynamic learning rate
        this.GeneratorOptimiser: Adam = Adam(this.Generator.parameters(),
            lr = TrainingSetting.LR_GENERATOR, betas = TrainingSetting.BETA_GENERATOR)
        this.DiscriminatorOptimiser: Adam = Adam(this.Discriminator.parameters(),
            lr = TrainingSetting.LR_DISCRIMINATOR, betas = TrainingSetting.BETA_DISCRIMINATOR)

        # parameters to be updated during training
        this.Epoch: int = 0
        this.GlobalStep: int = 0
        """
        The number of training step run in total, one training iteration is one global step; only updated during training.
        """
        this.Criterion: BCELoss = BCELoss()

        # allocated memory
        this.Label: Tensor = torch.zeros((TrainingSetting.BATCH_SIZE), dtype = torch.float32)

    def __del__(this):
        this.Summary.close()

    @classmethod
    def loadFrom(cls, model_name: str):
        """
        @brief Load a trainer from a saved model.

        @param model_name The name of the saved model.
        """
        # load saved data
        model = torch.load(DatasetSetting.MODEL_OUTPUT_PATH + '/' + model_name)
        trainer: cls = cls(model["log_name"])

        # load each member data
        trainer.Generator.load_state_dict(model["generator"])
        trainer.Discriminator.load_state_dict(model["discriminator"])

        trainer.GeneratorOptimiser.load_state_dict(model["generator_optimiser"])
        trainer.DiscriminatorOptimiser.load_state_dict(model["discriminator_optimiser"])

        trainer.Epoch = model["epoch"]
        trainer.GlobalStep = model["global_step"]
        trainer.Criterion.load_state_dict(model["criterion"])
        return trainer
    
    @staticmethod
    def normaliseNote(note: Tensor) -> Tensor:
        """
        @brief Normalise integer note to signed normalised range.

        @param note Note in range [0, NoteFeatureSize]
        @return Note in range [-1.0, 1.0]
        """
        return note.float() / EmbeddingSetting.NOTE_ORIGINAL_FEATURE_SIZE * 2.0 - 1.0
    
    @staticmethod
    def shiftTarget(target: Tensor, mask_ref: CoderMask) -> Tuple[Tensor, Tensor]:
        """
        @brief Shift the target based on the transformer specification, and modify the mask accordingly.

        @param target The target input.
        @param mask_ref The mask, which will be modified in-place based on the target input.

        @return The shifted target input and target expected output.
        """
        # for input, exclude the last time window
        # for output, exclude the first one
        windowSkip: int = DataUtility.calcTimeStepLength(1)
        sequenceSkip: int = DataUtility.calcSequenceLength(1)

        mask_ref.TargetPadding = mask_ref.TargetPadding[:, :-sequenceSkip]
        mask_ref.TargetAttention = mask_ref.TargetAttention[:-sequenceSkip, :-sequenceSkip]
        return (target[:, :-windowSkip, :], target[:, windowSkip:, :])
    
    def checkpoint(this, model_name: str) -> None:
        """
        @brief Save the current state of the trainer to a file.

        @param module_name The name of the saving model.
        A datetime will be automatically appended to the end of the name.
        """
        checkpointPath: str = DatasetSetting.MODEL_OUTPUT_PATH
        if not os.path.exists(checkpointPath):
            os.makedirs(checkpointPath)

        time: str = str(datetime.datetime.today().strftime("%Y-%m-%d_%H-%M-%S"))

        this.Summary.flush()
        torch.save({
            "log_name" : this.LogName,

            "generator" : this.Generator.state_dict(),
            "discriminator" : this.Discriminator.state_dict(),

            "generator_optimiser" : this.GeneratorOptimiser.state_dict(),
            "discriminator_optimiser" : this.DiscriminatorOptimiser.state_dict(),

            "epoch" : this.Epoch,
            "global_step" : this.GlobalStep,
            "criterion" : this.Criterion.state_dict()
            # filename extension follows PyTorch's convention
        }, checkpointPath + '/' + model_name + '-' + time + ".tar")

    def setMode(this, mode: OperationMode) -> None:
        """
        @brief Set the model operation mode.

        @param mode The mode set to.
        """
        match(mode):
            case Trainer.OperationMode.INFERENCE:
                this.Generator.eval()
                this.Discriminator.eval()
            case Trainer.OperationMode.TRAIN:
                this.Generator.train()
                this.Discriminator.train()

    def advanceEpoch(this) -> None:
        """
        @brief Advance epoch counter by one.
        """
        this.Epoch += 1

    def train(this, dataLoader: DataLoader) -> None:
        """
        @brief Train the model for one epoch using a provided data loader.
        Note that it's application's responsibility to set the trainer to the correct mode.

        @param dataLoader The dataloader for which the model will be trained on.
        @see setMode()
        """
        for i, data in enumerate(dataLoader):
            fake, real, mask = data # source is robotic MIDI (fake), target is performance MIDI (real)
            realInput, realExpected = Trainer.shiftTarget(real, mask)

            batchSize: int = fake.size(0)
            label: Tensor = this.Label[:batchSize].detach()

            # ---------------- train discriminator --------------- #
            this.Discriminator.zero_grad()
            # train with all real batch
            label.fill_(Trainer.REAL_LABEL)
            # normalised data for discriminator
            score: Tensor = this.Discriminator(Trainer.normaliseNote(realExpected))
            # calculate loss on all real batch
            err_real: Tensor = this.Criterion(score, label)
            # calculate gradient of discriminator in backward pass
            err_real.backward()
            Dx: float = score.mean().item()

            # train with all fake batch, basically just run the generator as usual
            label.fill_(Trainer.FAKE_LABEL)
            generated: Tensor = this.Generator(fake, realInput, mask)
            # we consider everything from the generator is fake
            score: Tensor = this.Discriminator(generated.detach()) # prevent updating parameters on generator
            err_fake: Tensor = this.Criterion(score, label)
            # calculate gradient of this batch, sum with previous gradients
            err_fake.backward()
            DGz1: float = score.mean().item()
            # compute error of discriminator as a sum over real and fake batch
            err_discriminator: Tensor = err_real + err_fake
            this.DiscriminatorOptimiser.step()

            # ----------------- train generator ------------------- #
            this.Generator.zero_grad()
            # invert the label for the generator cost
            label.fill_(Trainer.REAL_LABEL)
            # run another forward pass on discriminator because we just updated it
            score: Tensor = this.Discriminator(generated)
            # calculate loss of generator
            err_generator: Tensor = this.Criterion(score, label)
            err_generator.backward()
            DGz2: float = score.mean().item()
            this.GeneratorOptimiser.step()

            # --------------------- logging ----------------------- #
            if i % TrainingSetting.LOG_FREQUENCY == 0:
                this.Summary.add_scalars("train", {
                    "Loss(D)" : err_discriminator.mean().item(),
                    "Loss(G)" : err_generator.mean().item(),
                    "D(x)" : Dx,
                    "D(G(z1))" : DGz1,
                    "D(G(z2))" : DGz2
                }, this.GlobalStep)
            this.GlobalStep += 1

    def validate(this, dataLoader: DataLoader) -> None:
        """
        @brief Validate the model for one epoch with a provided data loader.
        Like train, this function does not set the model mode.

        @param dataLoader The data loader used for validation.
        """
        with torch.no_grad():
            for i, data in enumerate(dataLoader):
                fake, real, mask = data
                realInput, realExpected = Trainer.shiftTarget(real, mask)

                batchSize = fake.size(0)
                label: Tensor = this.Label[:batchSize].detach()

                # --------------- validate discriminator -------------- #
                label.fill_(Trainer.REAL_LABEL)
                score: Tensor = this.Discriminator(Trainer.normaliseNote(realExpected))
                err_discriminator: Tensor = this.Criterion(score, label)
                Dx: float = score.mean().item()

                # ---------------- validate generator ----------------- #
                label.fill_(Trainer.FAKE_LABEL)
                generated: Tensor = this.Generator(fake, realInput, mask)
                score: Tensor = this.Discriminator(generated)
                err_generator: Tensor = this.Criterion(score, label)
                DGz: float = score.mean().item()

                # --------------------- logging ----------------------- #
                if i % TrainingSetting.LOG_FREQUENCY == 0:
                    this.Summary.add_scalars("validation", {
                        "Loss(D)" : err_discriminator,
                        "Loss(G)" : err_generator,
                        "D(x)" : Dx,
                        "D(G(z))" : DGz
                    }, this.GlobalStep)
import os
import torch
from torch import nn
import torch.nn.functional as F
from torch import optim
from torch.utils.data import DataLoader

from argparse import ArgumentParser
from data import PlanningDataset
from model import PlaningNetwork, MultipleTrajectoryPredictionLoss
import pytorch_lightning as pl
import matplotlib.pyplot as plt
from pytorch_lightning.callbacks import LearningRateMonitor


class PlanningBaselineV0(pl.LightningModule):
    def __init__(self, M, num_pts, mtp_alpha, lr) -> None:
        super().__init__()
        self.M = M
        self.num_pts = num_pts
        self.mtp_alpha = mtp_alpha
        self.lr = lr

        self.net = PlaningNetwork(M, num_pts)
        self.mtp_loss = MultipleTrajectoryPredictionLoss(mtp_alpha, M, num_pts, distance_type='angle')

    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = parent_parser.add_argument_group('PlanningBaselineV0')
        parser.add_argument('--M', type=int, default=3)
        parser.add_argument('--num_pts', type=int, default=20)
        parser.add_argument('--mtp_alpha', type=float, default=1.0)
        return parent_parser

    def forward(self, x):
        # in lightning, forward defines the prediction/inference actions
        return self.net(x)

    def training_step(self, batch, batch_idx):
        # training_step defines the train loop. It is independent of forward
        inputs, labels = batch['input_img'], batch['future_poses']
        pred_cls, pred_trajectory = self.net(inputs)
        cls_loss, reg_loss = self.mtp_loss(pred_cls, pred_trajectory, labels)
        self.log('loss/cls', cls_loss)
        self.log('loss/reg', reg_loss.mean())
        self.log('loss/reg_x', reg_loss[0])
        self.log('loss/reg_y', reg_loss[1])
        self.log('loss/reg_z', reg_loss[2])

        if batch_idx % 10 == 0:
            pred_trajectory = pred_trajectory.detach().cpu().numpy().reshape(-1, self.M, self.num_pts, 3)
            pred_cls = pred_cls.detach().cpu().numpy()
            fig, ax = plt.subplots()
            ax.plot(-pred_trajectory[0, 0, :, 1], pred_trajectory[0, 0, :, 0], 'o-', label='pred0 - conf %.3f' % pred_cls[0, 0])
            ax.plot(-pred_trajectory[0, 1, :, 1], pred_trajectory[0, 1, :, 0], 'o-', label='pred1 - conf %.3f' % pred_cls[0, 1])
            ax.plot(-pred_trajectory[0, 2, :, 1], pred_trajectory[0, 2, :, 0], 'o-', label='pred2 - conf %.3f' % pred_cls[0, 2])
            ax.plot(-labels.detach().cpu().numpy()[0, :, 1], labels.detach().cpu().numpy()[0, :, 0], 'o-', label='gt')
            plt.legend()
            plt.tight_layout()
            self.logger.experiment.add_figure('test', plt.gcf(), self.global_step)
            plt.close(fig)

        return cls_loss + self.mtp_alpha * reg_loss.mean()

    def configure_optimizers(self):
        optimizer = optim.Adam(self.parameters(), lr=self.lr, weight_decay=0.01)
        lr_scheduler = optim.lr_scheduler.StepLR(optimizer, 10, 0.9)
        # optimizer = optim.SGD(self.parameters(), lr=LR, momentum=0.9, weight_decay=0.01)
        return [optimizer], [lr_scheduler]

    def validation_step(self, batch, batch_idx):
        inputs, labels = batch['input_img'], batch['future_poses']
        pred_cls, pred_trajectory = self.net(inputs)
        bs = len(pred_cls)
        pred_trajectory = pred_trajectory.reshape(-1, self.M, self.num_pts, 3)  # B, M, num_pts, 3

        pred_label = torch.argmax(pred_cls, -1)

        # Prediction L2 loss
        pred_trajectory_single = pred_trajectory[torch.tensor(range(bs), device=pred_cls.device), pred_label, ...]
        l2_dist = F.mse_loss(pred_trajectory_single, labels)

        # cls Acc
        gt_trajectory_M = labels[:, None, ...].expand(-1, self.M, -1, -1)
        l2_distances = F.mse_loss(pred_trajectory, gt_trajectory_M, reduction='none').sum(dim=(2, 3))  # B, M
        best_match = torch.argmin(l2_distances, -1)
        cls_acc = torch.sum(best_match == pred_label) / bs

        self.log_dict({'val/l2_dist': l2_dist, 'val/cls_acc': cls_acc})


if __name__ == "__main__":

    parser = ArgumentParser()
    
    parser.add_argument('--batch_size', type=int, default=32)
    parser.add_argument('--lr', type=float, default=1e-4)
    parser.add_argument('--n_workers', type=int, default=8)

    parser = PlanningBaselineV0.add_model_specific_args(parser)
    parser = pl.Trainer.add_argparse_args(parser)
    
    args = parser.parse_args()

    train = PlanningDataset(split='train')
    val = PlanningDataset(split='val')
    train_loader = DataLoader(train, args.batch_size, shuffle=True, num_workers=args.n_workers)
    val_loader = DataLoader(val, args.batch_size, num_workers=args.n_workers)

    planning_v0 = PlanningBaselineV0(args.M, args.num_pts, args.mtp_alpha, args.lr)
    lr_monitor = LearningRateMonitor(logging_interval='step')

    trainer = pl.Trainer.from_argparse_args(args,
                                            accelerator='ddp',
                                            profiler='simple',
                                            benchmark=True,
                                            log_every_n_steps=10,
                                            flush_logs_every_n_steps=50,
                                            callbacks=[lr_monitor],
                                            )

    trainer.fit(planning_v0, train_loader, val_loader)

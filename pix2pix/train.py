import os
import pathlib
import time
import sys
import argparse

import torch
import torch.optim as optim
import torchvision
from torch.utils.tensorboard import SummaryWriter
from torchvision import transforms
from torch.utils.data import DataLoader

from ops import constants
from networks import pix2pix_networks as p2p_net
from utils.dataloader_paired import PairedImg2ImgDataset
from ops import paired_image_transforms


def main(args):
    print(args)
    device = torch.device(
        "cuda:1" if (torch.cuda.is_available() and args.ngpu > 0)
        else "cpu")
    print('Device:', device)

    lossfn_rec = constants.RECONSTRUCTION_LOSS[args.rec_loss]
    lossfn_G, lossfn_D_real, lossfn_D_fake = constants.GAN_LOSS[args.gan_loss]

    generator = p2p_net.Pix2PixGenerator(n_filters=args.nf).to(device)
    discriminator = p2p_net.Pix2PixDiscriminator(n_filters=args.nf).to(device)

    optimizer_G = optim.Adam(
        generator.parameters(), lr=args.learning_rate,
        betas=(args.beta1, 0.9))
    optimizer_D = optim.Adam(
        discriminator.parameters(), lr=args.learning_rate,
        betas=(args.beta1, 0.9))

    def training_step_G(batch_x, batch_y):
        generator.zero_grad()

        output = generator(batch_x)

        rec_loss = lossfn_rec(input=output, target=batch_y)

        input_fake = torch.cat([batch_x, output], axis=1)
        d_fake = discriminator(input_fake)
        err_G = lossfn_G(d_fake)

        loss_g = args.lambda_rec * rec_loss + err_G

        loss_g.backward()
        optimizer_G.step()

        return {'rec': rec_loss.cpu().item(),
                'errG': err_G.cpu().item()}

    def training_step_D(batch_x, batch_y):
        discriminator.zero_grad()

        input_real = torch.cat([batch_x, batch_y], axis=1)
        d_real = discriminator(input_real)
        err_D_real = lossfn_D_real(d_real)
        err_D_real.backward()

        output = generator(batch_x)

        input_fake = torch.cat([batch_x, output], axis=1)
        d_fake = discriminator(input_fake)
        err_D_fake = lossfn_D_fake(d_fake)
        err_D_fake.backward()

        err_D = err_D_real + err_D_fake

        # err_D.backward()
        optimizer_D.step()

        return {'errD_real': err_D_real.cpu().item(),
                'errD_fake': err_D_fake.cpu().item(),
                'errD': err_D.cpu().item()}

    paired_tsfm = transforms.Compose([
        paired_image_transforms.RandomPairedHFlip(prob=0.5)])

    tsfm = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize(
            mean=(0.5, 0.5, 0.5),
            std=(0.5, 0.5, 0.5))
    ])

    dataset = PairedImg2ImgDataset(
        image_dir=args.train_path,
        paired_transform=paired_tsfm,
        transform=tsfm,
        mode='train')

    dataloader = DataLoader(
        dataset, batch_size=args.batch_size,
        shuffle=True, num_workers=args.num_workers)

    val_dataset = PairedImg2ImgDataset(
        image_dir=args.train_path,
        paired_transform=paired_tsfm,
        transform=tsfm,
        mode='val')

    val_dataloader = DataLoader(
        val_dataset, batch_size=16,
        shuffle=False, num_workers=args.num_workers)

    static_val_batch_x, static_val_batch_y, _ = next(iter(val_dataloader))

    if args.y2x:
        grid_static_input = torchvision.utils.make_grid(
            static_val_batch_y * 0.5 + 0.5, nrow=4)

        grid_static_target = torchvision.utils.make_grid(
            static_val_batch_x * 0.5 + 0.5, nrow=4)
    else:
        grid_static_input = torchvision.utils.make_grid(
            static_val_batch_x * 0.5 + 0.5, nrow=4)

        grid_static_target = torchvision.utils.make_grid(
            static_val_batch_y * 0.5 + 0.5, nrow=4)

    writer = SummaryWriter(log_dir=os.path.join(args.checkpoint_dir, 'log/'))

    for epoch in range(1, args.num_epochs+1):

        for i, (batch_x, batch_y) in enumerate(dataloader):

            if not args.y2x:
                batch_x_dev = batch_x.to(device)
                batch_y_dev = batch_y.to(device)
            else:  # swap the x & y
                batch_x_dev = batch_y.to(device)
                batch_y_dev = batch_x.to(device)

            losses_g = training_step_G(batch_x_dev, batch_y_dev)

            losses_d = training_step_D(batch_x_dev, batch_y_dev)

            if i % args.log_interval == 0:
                print('Epoch {:<3d}/{} Iter {:>3d}/{} Rec: {:.4f} G: {:.4f} D: {:.4f}'
                      ''.format(epoch, args.num_epochs, i, len(dataloader),
                                losses_g['rec'], losses_g['errG'],
                                losses_d['errD']))

            writer.add_scalar(
                'loss/errG', losses_g['errG'], global_step=epoch)
            writer.add_scalar(
                'loss/rec-{}'.format(args.rec_loss), losses_g['rec'],
                global_step=epoch)
            writer.add_scalar(
                'loss/errD', losses_d['errD'], global_step=epoch)

        if epoch % 10 == 0:
            torch.save(
                {
                    'epoch': epoch,
                    'model_state_dict': generator.state_dict()
                },
                os.path.join(
                    args.checkpoint_dir,
                    'model-{}.tch'.format(epoch)
                )
            )

        gen_images = generator(batch_x_dev)
        grid_generated = torchvision.utils.make_grid(
            gen_images * 0.5 + 0.5, nrow=4)
        if not args.y2x:
            grid_target = torchvision.utils.make_grid(batch_y*0.5+0.5, nrow=4)
        else:
            grid_target = torchvision.utils.make_grid(batch_x*0.5+0.5, nrow=4)
        writer.add_image('images/trainset-target', grid_target, epoch)
        writer.add_image('images/trainset-generated', grid_generated, epoch)

        # validation-set:
        generator.eval()
        if args.y2x:
            val_outputs = generator(static_val_batch_y.to(device))
        else:
            val_outputs = generator(static_val_batch_x.to(device))

        val_outputs = val_outputs.detach().cpu()

        grid_generated = torchvision.utils.make_grid(
            val_outputs * 0.5 + 0.5, nrow=4)

        writer.add_image('images/val-set-input', grid_static_input, epoch)
        writer.add_image('images/val-set-target', grid_static_target, epoch)
        writer.add_image('images/val-set-generated', grid_generated, epoch)
        generator.train()

    return losses_g, losses_d


def parse(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument(
            '--train_path', type=str, required=True,
            help='The path to the training directory')
    parser.add_argument(
            '--y2x', type=int, default=0,
            help='Transforming y to x (instead of x to y)')
    parser.add_argument(
            '--ngpu', type=int, default=1,
            help='Number of GPUs to use')
    parser.add_argument(
            '--nf', type=int, required=True,
            help='Number of filters for the networks')
    parser.add_argument(
            '--gan_loss', type=str, required=True,
            choices=['vanilla', 'wgan'],
            help='The type of GAN loss to sue for training')
    parser.add_argument(
            '--rec_loss', required=True,
            choices=['l1', 'l2', 'ce'],
            help='The type of reconstruction loss')
    parser.add_argument(
            '--lambda_rec', type=float, default=100.0,
            help='Lambda for the weight of reconstruction loss')
    parser.add_argument(
            '--batch_size', type=int, default=16,
            help='Batch size of training')
    parser.add_argument(
            '--num_workers', type=int, default=1,
            help='Number of workers for loading data')
    parser.add_argument(
            '--learning_rate', type=float, default=2e-4,
            help='Learning rate')
    parser.add_argument(
            '--num_epochs', type=int, default=50,
            help='Number of epochs for training the model')
    parser.add_argument(
            '--log_interval', type=int, default=100,
            help='The interval steps to print the loss values')
    parser.add_argument(
            '--beta1', type=float, default=0.5,
            help='Bets for the Adam optimizer')
    parser.add_argument(
            '--checkpoint_dir', type=str, default='/tmp/checkpoints/',
            help='Checkpoint directory for training')

    args = parser.parse_args(argv)

    args.y2x = bool(args.y2x)

    if args.checkpoint_dir == '/tmp/checkpoints/':
        timestr = time.strftime('%m_%d_%Y-%H_%M_%S')
        args.checkpoint_dir = os.path.join(args.checkpoint_dir, timestr)

    ckpt_path = pathlib.Path(args.checkpoint_dir)
    ckpt_path.mkdir(parents=True)

    return args


if __name__ == '__main__':
    args = parse(sys.argv[1:])
    main(args)

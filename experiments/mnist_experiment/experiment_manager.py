import argparse
import os

import torch
from torch import optim
from torch.distributions import Bernoulli
from torch.distributions.kl import kl_divergence
from torchvision.utils import save_image

from boilr import VIExperimentManager
from boilr.utils import img_grid_pad_value
from models.mnist_vae import MnistVAE
from .data import DatasetManager


class MnistExperiment(VIExperimentManager):
    """
    Experiment manager.

    Data attributes:
    - 'args': argparse.Namespace containing all config parameters. When
      initializing this object, if 'args' is not given, all config
      parameters are set based on experiment defaults and user input, using
      argparse.
    - 'run_description': string description of the run that includes a timestamp
      and can be used e.g. as folder name for logging.
    - 'model': the model.
    - 'device': torch.device that is being used
    - 'dataloaders': DataLoaders, with attributes 'train' and 'test'
    - 'optimizer': the optimizer
    """

    def make_datamanager(self):
        cuda = self.device.type == 'cuda'
        return DatasetManager(self.args, cuda)

    def make_model(self):
        return MnistVAE()

    def make_optimizer(self):
        return optim.Adam(self.model.parameters(),
                          lr=self.args.lr,
                          weight_decay=self.args.weight_decay)


    def forward_pass(self, model, x, y=None):
        """
        Simple single-pass model evaluation. It consists of a forward pass
        and computation of all necessary losses and metrics.
        """

        x = x.to(self.device, non_blocking=True)
        out = model(x)

        pxz = Bernoulli(out['mean'])
        log_likelihood = pxz.log_prob(x).sum((1, 2, 3))
        kl = kl_divergence(out['qz'], model.pz).sum(1)
        elbo_sep = log_likelihood - kl
        elbo = elbo_sep.mean()
        loss = - elbo

        out = {
            'out_sample': out['sample'],
            'out_mean': out['mean'],
            'loss': loss,
            'elbo_sep': elbo_sep,
            'elbo': elbo,
            'recons': -log_likelihood.mean(),
            'kl': kl.mean(),
        }
        return out


    @staticmethod
    def print_train_log(step, epoch, summaries):
        s = ("       [step {step}]   loss: {loss:.5g}   ELBO: {elbo:.5g}   "
             "recons: {recons:.3g}   KL: {kl:.3g}")
        s = s.format(
            step=step,
            loss=summaries['loss/loss'],
            elbo=summaries['elbo/elbo'],
            recons=summaries['elbo/recons'],
            kl=summaries['elbo/kl'],
        )
        print(s)


    @staticmethod
    def print_test_log(summaries, step=None, epoch=None):
        log_string = "       "
        if epoch is not None:
            log_string += "[step {}, epoch {}]   ".format(step, epoch)
        s = "ELBO {elbo:.5g}   recons: {recons:.3g}   KL: {kl:.3g}"
        log_string += s.format(
            elbo=summaries['elbo/elbo'],
            recons=summaries['elbo/recons'],
            kl=summaries['elbo/kl'])
        ll_key = None
        for k in summaries.keys():
            if k.find('elbo_IW') > -1:
                ll_key = k
                iw_samples = k.split('_')[-1]
                break
        if ll_key is not None:
            log_string += "   marginal log-likelihood ({}) {:.5g}".format(
                iw_samples, summaries[ll_key])

        print(log_string)


    @staticmethod
    def get_metrics_dict(results):
        metrics_dict = {
            'loss/loss': results['loss'].item(),
            'elbo/elbo': results['elbo'].item(),
            'elbo/recons': results['recons'].item(),
            'elbo/kl': results['kl'].item(),
        }
        return metrics_dict


    def additional_testing(self, img_folder):
        """
        Perform additional testing, including possibly generating images.

        In this case, save samples from the generative model, and pairs
        input/reconstruction from the test set.

        :param img_folder: folder to store images
        """

        step = self.model.global_step

        if not self.args.dry_run:

            # Saved images will have n**2 sub-images
            n = 8

            # Save model samples
            sample = self.model.sample_prior(n ** 2)
            fname = os.path.join(img_folder, 'sample_' + str(step) + '.png')
            pad = img_grid_pad_value(sample)
            save_image(sample, fname, nrow=n, pad_value=pad)

            # Get first test batch
            (x, _) = next(iter(self.dataloaders.test))
            fname = os.path.join(img_folder, 'reconstruction_' + str(step) + '.png')

            # Save model original/reconstructions
            self.save_input_and_recons(x, fname, n)


    def save_input_and_recons(self, x, fname, n):
        n_img = n ** 2 // 2
        if x.shape[0] < n_img:
            msg = ("{} data points required, but given batch has size {}. "
                   "Please use a larger batch.".format(n_img, x.shape[0]))
            raise RuntimeError(msg)
        x = x.to(self.device)
        outputs = self.forward_pass(self.model, x)
        x = x[:n_img]
        recons = outputs['out_sample'][:n_img]
        imgs = torch.stack([x.cpu(), recons.cpu()])
        imgs = imgs.permute(1, 0, 2, 3, 4)
        imgs = imgs.reshape(n ** 2, x.size(1), x.size(2), x.size(3))
        pad = img_grid_pad_value(imgs)
        save_image(imgs, fname, nrow=n, pad_value=pad)


    def _parse_args(self):
        """
        Parse command-line arguments defining experiment settings.

        :return: args: argparse.Namespace with experiment settings
        """
        parser = argparse.ArgumentParser(
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            allow_abbrev=False)

        self.add_required_args(parser,

                               # General
                               batch_size=64,
                               test_batch_size=1000,
                               lr=1e-3,
                               seed=54321,
                               log_interval=1000,
                               test_log_interval=1000,
                               checkpoint_interval=10000,
                               resume="",

                               # VI-specific
                               ll_every=50000,
                               loglik_samples=100,)

        parser.add_argument('--wd',
                            type=float,
                            default=0.0,
                            dest='weight_decay',
                            help='weight decay')

        args = parser.parse_args()

        assert args.loglik_interval % args.test_log_interval == 0

        return args

    @staticmethod
    def _make_run_description(args):
        """
        Create a string description of the run. It is used in the names of the
        logging folders.

        :param args: experiment config
        :return: the run description
        """
        s = ''
        s += 'seed{}'.format(args.seed)
        if len(args.additional_descr) > 0:
            s += ',' + args.additional_descr
        return s

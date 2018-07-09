# -*- coding: utf-8 -*-
import codecs
import functools
import json

import numpy as np
import tensorflow as tf
from sklearn.metrics import accuracy_score
from tensorflow.contrib.framework import arg_scope, add_arg_scope

from tfsnippet.bayes import BayesianNet
from tfsnippet.dataflow import DataFlow
from tfsnippet.distributions import Normal, Bernoulli, Categorical
from tfsnippet.examples.nn import (resnet_block,
                                   deconv_resnet_block,
                                   reshape_conv2d_to_flat,
                                   l2_regularizer,
                                   regularization_loss,
                                   conv2d,
                                   batch_norm_2d,
                                   dense)
from tfsnippet.examples.utils import (load_mnist,
                                      create_session,
                                      Config,
                                      anneal_after,
                                      save_images_collection,
                                      Results,
                                      MultiGPU,
                                      get_batch_size,
                                      flatten,
                                      unflatten,
                                      int_shape, collect_outputs)
from tfsnippet.scaffold import TrainLoop
from tfsnippet.trainer import AnnealingDynamicValue, LossTrainer, Evaluator
from tfsnippet.utils import global_reuse, get_default_session_or_error


class ExpConfig(Config):
    # model parameters
    x_dim = 784
    z_dim = 32
    n_clusters = 16
    channels_last = False
    gaussian_mixture_prior = 'unit'  # choices: {'unit', '2d_balls'}
    mean_field_assumption_for_q = True

    # training parameters
    max_epoch = 3000
    batch_size = 128
    l2_reg = 0.0001
    initial_lr = 0.001
    lr_anneal_factor = 0.5
    lr_anneal_epoch_freq = 300
    lr_anneal_step_freq = None
    train_n_samples = 50

    # evaluation parameters
    test_n_samples = 500
    test_batch_size = 128


def gaussian_mixture_prior(y, z_dim, n_clusters):
    if config.gaussian_mixture_prior == 'unit':
        y = tf.stop_gradient(y)
        if None not in int_shape(y):
            z_shape = int_shape(y) + (z_dim,)
        else:
            z_shape = tf.concat([tf.shape(y), [z_dim]], axis=0)
        return Normal(mean=tf.zeros(z_shape), std=tf.ones(z_shape))
    elif config.gaussian_mixture_prior == '2d_balls':
        theta = 2 * np.pi / n_clusters
        R = np.sqrt(18. / (1 - np.cos(theta)))
        y_float = tf.to_float(tf.stop_gradient(y))
        z_prior_mean = tf.stack(
            [R * tf.cos(y_float * theta), R * tf.sin(y_float * theta)] +
            [tf.zeros_like(y_float)] * (z_dim - 2),
            axis=-1
        )
        z_prior_std = tf.ones([1, z_dim])
        return Normal(mean=z_prior_mean, std=z_prior_std)
    else:
        raise ValueError(
            'Unexpected value for config `gaussian_mixture_prior`: {}'.
            format(config.gaussian_mixture_prior)
        )


@global_reuse
@add_arg_scope
def q_net(x, observed=None, n_samples=None, is_training=True,
          channels_last=False):
    net = BayesianNet(observed=observed)

    # compute the hidden features
    with arg_scope([dense],
                   activation_fn=tf.nn.leaky_relu,
                   kernel_regularizer=l2_regularizer(config.l2_reg)):
        h_x = tf.to_float(x)
        h_x = dense(h_x, 500)
        h_x = dense(h_x, 500)

    # sample y ~ q(y|x)
    y_logits = dense(h_x, config.n_clusters, name='y_logits')
    y = net.add('y', Categorical(y_logits), n_samples=n_samples)

    # sample z ~ q(z|y,x)
    with arg_scope([dense],
                   activation_fn=tf.nn.leaky_relu,
                   kernel_regularizer=l2_regularizer(config.l2_reg)):
        if config.mean_field_assumption_for_q:
            # by mean-field-assumption we let q(z|y,x) = q(z|x)
            h_z, s1, s2 = flatten(h_x, 2)
            z_n_samples = n_samples
        else:
            if n_samples is not None:
                h_z = tf.concat(
                    [
                        tf.tile(tf.reshape(h_x, [1, -1, 500]),
                                tf.stack([n_samples, 1, 1])),
                        tf.one_hot(y, config.n_clusters)
                    ],
                    axis=-1
                )
            else:
                h_z = tf.concat([h_x, tf.one_hot(y, config.n_clusters)],
                                axis=-1)
            h_z, s1, s2 = flatten(h_z, 2)
            h_z = dense(h_z, 100, activation_fn=tf.nn.relu)
            z_n_samples = None

    z_mean = dense(h_z, config.z_dim, name='z_mean')
    z_logstd = dense(h_z, config.z_dim, name='z_logstd')
    z = net.add('z',
                Normal(mean=unflatten(z_mean, s1, s2),
                       logstd=unflatten(z_logstd, s1, s2),
                       is_reparameterized=False),
                n_samples=z_n_samples, group_ndims=1)

    return net


@global_reuse
@add_arg_scope
def p_net(observed=None, n_samples=None, is_training=True,
          channels_last=False):
    net = BayesianNet(observed=observed)

    # sample y
    y = net.add('y',
                Categorical(tf.zeros([1, config.n_clusters])),
                n_samples=n_samples)

    # sample z ~ p(z|y)
    z = net.add('z',
                gaussian_mixture_prior(y, config.z_dim, config.n_clusters),
                group_ndims=1)

    # compute the hidden features for x
    with arg_scope([dense],
                   activation_fn=tf.nn.leaky_relu,
                   kernel_regularizer=l2_regularizer(config.l2_reg)):
        h_x, s1, s2 = flatten(z, 2)
        h_x = dense(h_x, 500)
        h_x = dense(h_x, 500)

    # sample x ~ p(x|z)
    x_logits = unflatten(dense(h_x, config.x_dim, name='x_logits'), s1, s2)
    x = net.add('x', Bernoulli(logits=x_logits), group_ndims=1)

    return net


def sample_from_probs(x):
    uniform_samples = tf.random_uniform(
        shape=tf.shape(x), minval=0., maxval=1.,
        dtype=x.dtype
    )
    return tf.cast(tf.less(uniform_samples, x), dtype=tf.int32)


def main():
    # load mnist data
    (x_train, y_train), (x_test, y_test) = \
        load_mnist(shape=[config.x_dim], dtype=np.float32, normalize=True)

    # input placeholders
    input_x = tf.placeholder(
        dtype=tf.int32, shape=(None,) + x_train.shape[1:], name='input_x')
    is_training = tf.placeholder(
        dtype=tf.bool, shape=(), name='is_training')
    learning_rate = tf.placeholder(shape=(), dtype=tf.float32)
    learning_rate_var = AnnealingDynamicValue(config.initial_lr,
                                              config.lr_anneal_factor)
    multi_gpu = MultiGPU(disable_prebuild=False)

    # build the model
    grads = []
    losses = []
    lower_bounds = []
    test_nlls = []
    y_given_x_list = []
    batch_size = get_batch_size(input_x)
    params = None
    optimizer = tf.train.AdamOptimizer(learning_rate)

    for dev, pre_build, [dev_input_x] in multi_gpu.data_parallel(
            batch_size, [input_x]):
        with tf.device(dev), multi_gpu.maybe_name_scope(dev):
            if pre_build:
                with arg_scope([q_net, p_net], channels_last=True,
                               is_training=is_training):
                    _ = q_net(dev_input_x).chain(
                        p_net,
                        latent_names=['y', 'z'],
                        observed={'x': dev_input_x}
                    )

            else:
                with arg_scope([q_net, p_net],
                               channels_last=config.channels_last,
                               is_training=is_training):
                    # derive the loss and lower-bound for training
                    train_q_net = q_net(
                        dev_input_x, n_samples=config.train_n_samples
                    )
                    train_chain = train_q_net.chain(
                        p_net, latent_names=['y', 'z'], latent_axis=0,
                        observed={'x': dev_input_x}
                    )
                    dev_vae_loss = tf.reduce_mean(
                        train_chain.vi.training.vimco())
                    dev_loss = dev_vae_loss + regularization_loss()
                    dev_lower_bound = -dev_vae_loss
                    losses.append(dev_loss)
                    lower_bounds.append(dev_lower_bound)

                    # derive the nll and logits output for testing
                    test_q_net = q_net(
                        dev_input_x, n_samples=config.test_n_samples
                    )
                    test_chain = test_q_net.chain(
                        p_net, latent_names=['y', 'z'], latent_axis=0,
                        observed={'x': dev_input_x}
                    )
                    dev_test_nll = -tf.reduce_mean(
                        test_chain.vi.evaluation.is_loglikelihood())
                    test_nlls.append(dev_test_nll)

                    # derive the classifier via q(y|x)
                    dev_q_y_given_x = tf.argmax(
                        train_q_net['y'].distribution.logits, axis=-1)
                    y_given_x_list.append(dev_q_y_given_x)

                    # derive the optimizer
                    params = tf.trainable_variables()
                    grads.append(
                        optimizer.compute_gradients(dev_loss, var_list=params))

    # merge multi-gpu outputs and operations
    [loss, lower_bound, test_nll] = \
        multi_gpu.average([losses, lower_bounds, test_nlls], batch_size)
    [y_given_x] = multi_gpu.concat([y_given_x_list])

    train_op = multi_gpu.apply_grads(
        grads=multi_gpu.average_grads(grads),
        optimizer=optimizer,
        control_inputs=tf.get_collection(tf.GraphKeys.UPDATE_OPS)
    )

    # derive the plotting function
    with tf.device(multi_gpu.main_device), tf.name_scope('plot_x'):
        plot_p_net = p_net(n_samples=100, is_training=is_training,
                           channels_last=config.channels_last)
        x_plots = tf.reshape(
            tf.cast(
                255 * tf.sigmoid(plot_p_net['x'].distribution.logits),
                dtype=tf.uint8
            ),
            [-1, 28, 28]
        )

    def plot_samples(loop):
        with loop.timeit('plot_time'):
            session = get_default_session_or_error()
            images = session.run(x_plots, feed_dict={is_training: False})
            save_images_collection(
                images=images,
                filename=results.prepare_parent('plotting/{}.png'.
                                                format(loop.epoch)),
                grid_size=(10, 10)
            )

    # derive the final un-supervised classifier
    cluster_to_label = [None, None]

    def train_classifier(loop):
        with loop.timeit('cls_train_time'):
            [c_pred] = collect_outputs(
                outputs=[y_given_x],
                inputs=[input_x],
                data_flow=train_flow,
                feed_dict={is_training: False}
            )
            cluster_probs = np.array([
                np.mean(c_pred == i) for i in range(config.n_clusters)])
            y_true = y_train
            probs = np.zeros([config.n_clusters, 10])
            for c, t in zip(c_pred, y_true):
                probs[c, t] += 1
            probs = probs / np.maximum(np.sum(probs, axis=-1, keepdims=True), 1)
            labels = np.argmax(probs, axis=-1)
            cluster_to_label[:] = probs, labels
            print('> Cluster probs: [{}]'.format(
                ', '.join('{:.4g}'.format(p) for p in cluster_probs)))
            print('  Cluster labels: {}'.format(labels.tolist()))
            print('  Cluster label probs:')
            for i, label_prob in enumerate(probs):
                print('    {}: [{}]'.format(
                    i, ', '.join('{:.4g}'.format(p) for p in label_prob)))

    def evaluate_classifier(loop):
        with loop.timeit('cls_test_time'):
            [c_pred] = collect_outputs(
                outputs=[y_given_x],
                inputs=[input_x],
                data_flow=test_flow,
                feed_dict={is_training: False}
            )
            y_pred = cluster_to_label[1][c_pred]
            y_true = y_test
            loop.collect_metrics({'test_acc': accuracy_score(y_true, y_pred)})

    # prepare for training and testing data
    def input_x_sampler(x):
        sess = get_default_session_or_error()
        return sess.run([sampled_x], feed_dict={sample_input_x: x})

    with tf.device('/device:CPU:0'):
        sample_input_x = tf.placeholder(
            dtype=tf.float32, shape=(None, config.x_dim), name='sample_input_x')
        sampled_x = sample_from_probs(sample_input_x)

    train_flow = DataFlow.arrays([x_train], config.batch_size, shuffle=True,
                                 skip_incomplete=True).map(input_x_sampler)
    test_flow = DataFlow.arrays([x_test], config.test_batch_size). \
        map(input_x_sampler)

    with create_session().as_default():
        # fix the testing flow, reducing the testing time
        test_flow = test_flow.to_arrays_flow(batch_size=config.test_batch_size)

        # train the network
        with TrainLoop(params,
                       max_epoch=config.max_epoch,
                       summary_dir=results.make_dir('train_summary'),
                       early_stopping=False) as loop:
            trainer = LossTrainer(
                loop, loss, train_op, [input_x], train_flow,
                feed_dict={learning_rate: learning_rate_var, is_training: True}
            )
            anneal_after(
                trainer, learning_rate_var, epochs=config.lr_anneal_epoch_freq,
                steps=config.lr_anneal_step_freq
            )
            evaluator = Evaluator(
                loop,
                metrics={'test_nll': test_nll, 'test_lb': lower_bound},
                inputs=[input_x],
                data_flow=test_flow,
                feed_dict={is_training: False},
                time_metric_name='test_time'
            )
            trainer.evaluate_after_epochs(evaluator, freq=10)
            trainer.evaluate_after_epochs(
                functools.partial(plot_samples, loop), freq=10)
            trainer.evaluate_after_epochs(
                functools.partial(train_classifier, loop), freq=10)
            trainer.evaluate_after_epochs(
                functools.partial(evaluate_classifier, loop), freq=10)

            trainer.log_after_epochs(freq=1)
            trainer.run()

    # write the final results
    with codecs.open('cluster_labels.json', 'wb', 'utf-8') as f:
        f.write(json.dumps({
            'probs': cluster_to_label[0].tolist(),
            'labels': cluster_to_label[1].tolist(),
        }))
    results.commit(evaluator.last_metrics_dict)


if __name__ == '__main__':
    config = ExpConfig()
    results = Results()
    main()

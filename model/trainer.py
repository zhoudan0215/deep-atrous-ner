import numpy as np
import sugartensor as tf

from model.model import calculate_f1_metrics, max_model_name

__author__ = 'georgi.val.stoyan0v@gmail.com'


#
# borrowed from sugartensor, applied some bug fixes to allow table initialization
#

def classifier_train(**kwargs):
    r"""Trains the model.

    Args:
      **kwargs:
        optim: A name for optimizer. 'MaxProp' (default), 'AdaMax', 'Adam', 'RMSProp' or 'sgd'.
        loss: A 0-D `Tensor` containing the value to minimize.
        lr: A Python Scalar (optional). Learning rate. Default is .001.
        beta1: A Python Scalar (optional). Default is .9.
        beta2: A Python Scalar (optional). Default is .99.
        clip_grad_norm : A Python Scalar (optional). Default is 10

        save_dir: A string. The root path to which checkpoint and log files are saved.
          Default is `asset/train`.
        max_ep: A positive integer. Maximum number of epochs. Default is 1000.    
        ep_size: A positive integer. Number of Total batches in an epoch. 
          For proper display of log. Default is 1e5.    

        save_interval: A Python scalar. The interval of saving checkpoint files.
          By default, for every 600 seconds, a checkpoint file is written.
        log_interval: A Python scalar. The interval of recoding logs.
          By default, for every 60 seconds, logging is executed.
        max_keep: A positive integer. Maximum number of recent checkpoints to keep. Default is 5.
        keep_interval: A Python scalar. How often to keep checkpoints. Default is 1 hour.

        category: Scope name or list to train

        eval_metric: A list of tensors containing the value to evaluate. Default is [].

        tqdm: Boolean. If True (Default), progress bars are shown. If False, a series of loss
            will be shown on the console.

    """
    opt = tf.sg_opt(kwargs)
    assert opt.loss is not None, 'loss is mandatory.'

    # default training options
    opt += tf.sg_opt(optim='MaxProp', lr=0.001, beta1=0.9, beta2=0.99, category='', ep_size=100000, clip_grad_norm=10)

    # get optimizer
    train_op = sg_optim(opt.loss, optim=opt.optim, lr=0.001, clip_grad_norm=opt.clip_grad_norm,
                        beta1=opt.beta1, beta2=opt.beta2, category=opt.category)

    # for console logging
    loss_ = opt.loss

    # use only first loss when multiple GPU case
    if isinstance(opt.loss, (tuple, list)):
        loss_ = opt.loss[0]

    # define train function
    # noinspection PyUnusedLocal
    @sg_train_func
    def train_func(sess, arg):
        return sess.run([loss_, train_op])[0]

    # run train function
    train_func(**opt)


def sg_optim(loss, **kwargs):
    r"""Applies gradients to variables.
    Args:
        loss: A 0-D `Tensor` containing the value to minimize. list of 0-D tensor for Multiple GPU
        kwargs:
          optim: A name for optimizer. 'MaxProp' (default), 'AdaMax', 'Adam', 'RMSProp' or 'sgd'.
          lr: A Python Scalar (optional). Learning rate. Default is .001.
          beta1: A Python Scalar (optional). Default is .9.
          beta2: A Python Scalar (optional). Default is .99.
          momentum : A Python Scalar for RMSProp optimizer (optional). Default is 0.
          category: A string or string list. Specifies the variables that should be trained (optional).
            Only if the name of a trainable variable starts with `category`, it's value is updated.
            Default is '', which means all trainable variables are updated.
    """
    opt = tf.sg_opt(kwargs)

    # default training options
    opt += tf.sg_opt(optim='MaxProp', lr=0.001, beta1=0.9, beta2=0.99, momentum=0., category='')

    # select optimizer
    if opt.optim == 'MaxProp':
        optim = tf.sg_optimize.MaxPropOptimizer(learning_rate=opt.lr, beta2=opt.beta2)
    elif opt.optim == 'AdaMax':
        optim = tf.sg_optimize.AdaMaxOptimizer(learning_rate=opt.lr, beta1=opt.beta1, beta2=opt.beta2)
    elif opt.optim == 'Adam':
        optim = tf.train.AdamOptimizer(learning_rate=opt.lr, beta1=opt.beta1, beta2=opt.beta2)
    elif opt.optim == 'RMSProp':
        optim = tf.train.RMSPropOptimizer(learning_rate=opt.lr, decay=opt.beta1, momentum=opt.momentum)
    else:
        optim = tf.train.GradientDescentOptimizer(learning_rate=opt.lr)

    # get trainable variables
    if isinstance(opt.category, (tuple, list)):
        var_list = []
        for cat in opt.category:
            var_list.extend([t for t in tf.trainable_variables() if t.name.startswith(cat)])
    else:
        var_list = [t for t in tf.trainable_variables() if t.name.startswith(opt.category)]

    #
    # calc gradient
    #

    # multiple GPUs case
    if isinstance(loss, (tuple, list)):
        gradients = []
        # loop for each GPU tower
        for i, loss_ in enumerate(loss):
            # specify device
            with tf.device('/gpu:%d' % i):
                # give new scope only to operation
                with tf.name_scope('gpu_%d' % i):
                    # add gradient calculation operation for each GPU tower
                    gradients.append(tf.gradients(loss_, var_list))

        # averaging gradient
        gradient = []
        for grad in zip(*gradients):
            gradient.append(tf.add_n(grad) / len(loss))
    # single GPU case
    else:
        gradient = tf.gradients(loss, var_list)

    gradient, _ = tf.clip_by_global_norm(gradient, opt.clip_grad_norm)

    # gradient update op
    with tf.device('/gpu:0'):
        grad_var = [(g, v) for g, v in zip(gradient, var_list)]
        grad_op = optim.apply_gradients(grad_var, global_step=tf.sg_global_step())

    # add summary using last tower value
    for g, v in grad_var:
        # exclude batch normal statics
        if 'mean' not in v.name and 'variance' not in v.name \
                and 'beta' not in v.name and 'gamma' not in v.name:
            tf.sg_summary_gradient(v, g)

    # extra update ops within category ( for example, batch normal running stat update )
    if isinstance(opt.category, (tuple, list)):
        update_op = []
        for cat in opt.category:
            update_op.extend([t for t in tf.get_collection(tf.GraphKeys.UPDATE_OPS) if t.name.startswith(cat)])
    else:
        update_op = [t for t in tf.get_collection(tf.GraphKeys.UPDATE_OPS) if t.name.startswith(opt.category)]

    return tf.group(*([grad_op] + update_op))


def sg_train_func(func):
    r""" Decorates a function `func` as sg_train_func.

    Args:
        func: A function to decorate
    """

    @tf.wraps(func)
    def wrapper(**kwargs):
        r""" Manages arguments of `tf.sg_opt`.

        Args:
          **kwargs:
            lr: A Python Scalar (optional). Learning rate. Default is .001.

            save_dir: A string. The root path to which checkpoint and log files are saved.
              Default is `asset/train`.
            max_ep: A positive integer. Maximum number of epochs. Default is 1000.
            ep_size: A positive integer. Number of Total batches in an epoch.
              For proper display of log. Default is 1e5.

            save_interval: A Python scalar. The interval of saving checkpoint files.
              By default, for every 600 seconds, a checkpoint file is written.
            log_interval: A Python scalar. The interval of recoding logs.
              By default, for every 60 seconds, logging is executed.
            max_keep: A positive integer. Maximum number of recent checkpoints to keep. Default is 5.
            keep_interval: A Python scalar. How often to keep checkpoints. Default is 1 hour.

            eval_metric: A list of tensors containing the value to evaluate. Default is [].

            tqdm: Boolean. If True (Default), progress bars are shown. If False, a series of loss
                will be shown on the console.
        """
        opt = tf.sg_opt(kwargs)

        # default training options
        opt += tf.sg_opt(lr=0.001,
                         save_dir='asset/train',
                         max_ep=1000, ep_size=100000,
                         save_interval=600, log_interval=60,
                         eval_metric=[],
                         max_keep=5, keep_interval=1,
                         tqdm=True)

        # training epoch and loss
        epoch, loss = -1, None

        # checkpoint saver
        saver = tf.train.Saver(max_to_keep=opt.max_keep,
                               keep_checkpoint_every_n_hours=opt.keep_interval)

        # add evaluation summary
        for m in opt.eval_metric:
            tf.sg_summary_metric(m)

        # summary writer
        log_dir = opt.save_dir + '/run-%02d%02d-%02d%02d' % tuple(tf.time.localtime(tf.time.time()))[1:5]
        summary_writer = tf.summary.FileWriter(log_dir)

        # console logging function
        def console_log(sess_):
            if epoch >= 0:
                tf.sg_info('\tEpoch[%03d:gs=%d] - loss = %s' %
                           (epoch, sess_.run(tf.sg_global_step()),
                            ('NA' if loss is None else '%8.6f' % loss)))

        local_init_op = tf.group(tf.sg_phase().assign(True), tf.tables_initializer(), tf.local_variables_initializer())

        # create supervisor
        sv = tf.train.Supervisor(logdir=opt.save_dir,
                                 saver=saver,
                                 save_model_secs=opt.save_interval,
                                 summary_writer=summary_writer,
                                 save_summaries_secs=opt.log_interval,
                                 global_step=tf.sg_global_step(),
                                 local_init_op=local_init_op)

        # create session
        with sv.managed_session(config=tf.ConfigProto(allow_soft_placement=True)) as sess:

            # console logging loop
            if not opt.tqdm:
                sv.loop(opt.log_interval, console_log, args=(sess,))

            # get start epoch
            _step = sess.run(tf.sg_global_step())
            ep = _step // opt.ep_size

            best_f1 = 0
            # check if already finished
            if ep <= opt.max_ep:

                # logging
                tf.sg_info('Training started from epoch[%03d]-step[%d].' % (ep, _step))

                # epoch loop
                for ep in range(ep, opt.max_ep + 1):

                    # update epoch info
                    start_step = sess.run(tf.sg_global_step()) % opt.ep_size
                    epoch = ep

                    # create progressbar iterator
                    if opt.tqdm:
                        iterator = tf.tqdm(range(start_step, opt.ep_size), total=opt.ep_size, initial=start_step,
                                           desc='train', ncols=70, unit='b', leave=False)
                    else:
                        iterator = range(start_step, opt.ep_size)

                    # batch loop
                    for _ in iterator:

                        # exit loop
                        if sv.should_stop():
                            break

                        # call train function
                        batch_loss = func(sess, opt)

                        # loss history update
                        if batch_loss is not None and \
                                not np.isnan(batch_loss.all()) and not np.isinf(batch_loss.all()):
                            if loss is None:
                                loss = np.mean(batch_loss)
                            else:
                                loss = loss * 0.9 + np.mean(batch_loss) * 0.1

                    # log epoch information
                    console_log(sess)

                    f1_stat = show_metrics(sv, sess, opt.eval_metric[2], opt.eval_metric[3], ep, opt.val_ep_size,
                                              'val', use_tqdm=True)

                    if f1_stat > best_f1:
                        best_f1 = f1_stat

                        max_model_file = opt.save_dir + max_model_name

                        # save last version
                        saver.save(sess, max_model_file)
                        print("Improved F1 score, max model saved in file: %s" % max_model_file)

                        print('Test metrics:')
                        show_metrics(sv, sess, opt.test_metric[0], opt.test_metric[1], ep, opt.test_ep_size,
                                        'test', use_tqdm=True)

                # save last version
                saver.save(sess, opt.save_dir + '/model.ckpt', global_step=sess.run(tf.sg_global_step()))

                # logging
                tf.sg_info('Training finished at epoch[%d]-step[%d].' % (ep, sess.run(tf.sg_global_step())))
            else:
                tf.sg_info('Training already finished at epoch[%d]-step[%d].' %
                           (ep - 1, sess.run(tf.sg_global_step())))

    return wrapper


def show_metrics(sv, sess, predictions_op, labels_op, epoch, ep_size, name, use_tqdm=True):
    # create progressbar iterator
    if use_tqdm:
        iterator = tf.tqdm(range(0, ep_size), total=ep_size, initial=0, desc=name, ncols=70, unit='b', leave=False)
    else:
        iterator = range(0, ep_size)

    all_predicted, all_targets = [], []
    for _ in iterator:

        # exit loop
        if sv.should_stop():
            break

        predictions, targets = sess.run([predictions_op, labels_op])
        all_predicted.extend(predictions.flatten())
        all_targets.extend(targets.flatten())

    f1_separate_scores, f1_stat, _, _, _, _ = calculate_f1_metrics(all_predicted, all_targets)
    print('Epoch {} - f1 scores of the meaningful classes: {}'.format(epoch, f1_separate_scores))
    print('Epoch {} - total f1 score: {}'.format(epoch, f1_stat))

    return f1_stat

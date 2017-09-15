from tensorflow.examples.tutorials.mnist import input_data
import tensorflow as tf
import numpy as np
import time
import os

""" Helper Functions """
def safe_log_prob(x, eps=1e-8):
    return tf.log(tf.clip_by_value(x, eps, 1.0))


def safe_clip(x, eps=1e-8):
    return tf.clip_by_value(x, eps, 1.0)


def gs(x):
    return x.get_shape().as_list()


def softplus(x):
    '''
    Let m = max(0, x), then,

    sofplus(x) = log(1 + e(x)) = log(e(0) + e(x)) = log(e(m)(e(-m) + e(x-m)))
                         = m + log(e(-m) + e(x - m))

    The term inside of the log is guaranteed to be between 1 and 2.
    '''
    m = tf.maximum(tf.zeros_like(x), x)
    return m + tf.log(tf.exp(-m) + tf.exp(x - m))


def bernoulli_loglikelihood(b, log_alpha):
    return b * (-softplus(-log_alpha)) + (1 - b) * (-log_alpha - softplus(-log_alpha))


def bernoulli_loglikelihood_derivitive(b, log_alpha):
    assert gs(b) == gs(log_alpha)
    sna = tf.sigmoid(-log_alpha)
    return b * sna - (1-b) * (1 - sna)


def v_from_u(u, log_alpha, force_same=True):
    u_prime = tf.nn.sigmoid(-log_alpha)
    v_1 = (u - u_prime) / safe_clip(1 - u_prime)
    v_1 = tf.clip_by_value(v_1, 0, 1)
    v_1 = tf.stop_gradient(v_1)
    v_1 = v_1 * (1 - u_prime) + u_prime
    v_0 = u / safe_clip(u_prime)
    v_0 = tf.clip_by_value(v_0, 0, 1)
    v_0 = tf.stop_gradient(v_0)
    v_0 = v_0 * u_prime

    v = tf.where(u > u_prime, v_1, v_0)
    v = tf.check_numerics(v, 'v sampling is not numerically stable.')
    if force_same:
        v = v + tf.stop_gradient(-v + u)  # v and u are the same up to numerical errors
    return v


def reparameterize(log_alpha, noise):
    return log_alpha + safe_log_prob(noise) - safe_log_prob(1 - noise)

# def reparameterize_noise(noise):
#     return safe_log_prob(noise) - safe_log_prob(1 - noise)


def concrete_relaxation(log_alpha, noise, temp):
    z = log_alpha + safe_log_prob(noise) - safe_log_prob(1 - noise)
    return tf.sigmoid(z / temp)


def neg_elbo(x, samples, log_alphas_inf, log_alphas_gen, prior, log=False):
    assert len(samples) == len(log_alphas_inf) == len(log_alphas_gen)
    # compute log[q(b1|x)q(b2|b1)...q(bN|bN-1)]
    log_q_bs = []
    for b, log_alpha in zip(samples, log_alphas_inf):
        log_q_cur_given_prev = tf.reduce_sum(bernoulli_loglikelihood(b, log_alpha), axis=1)
        log_q_bs.append(log_q_cur_given_prev)
    log_q_b = tf.add_n(log_q_bs)
    # compute log[p(b1, ..., bN, x)]
    log_p_x_bs = []

    all_log_alphas_gen = list(reversed(log_alphas_gen)) + [prior]
    all_samples_gen = [x] + samples
    for b, log_alpha in zip(all_samples_gen, all_log_alphas_gen):
        log_p_next_given_cur = tf.reduce_sum(bernoulli_loglikelihood(b, log_alpha), axis=1)
        log_p_x_bs.append(log_p_next_given_cur)
    log_p_b_x = tf.add_n(log_p_x_bs)

    if log:
        for i, log_q in enumerate(log_q_bs):
            log_p = log_p_x_bs[i+1]
            kl = tf.reduce_mean(log_q - log_p)
            tf.summary.scalar("kl_{}".format(i), kl)
        tf.summary.scalar("log_p_x_given_b", tf.reduce_mean(log_p_x_bs[0]))
    return -1. * (log_p_b_x - log_q_b), log_q_bs


""" Networks """
def encoder(x, num_latents, name, reuse):
    with tf.variable_scope(name, reuse=reuse):
        log_alpha = tf.layers.dense(2. * x - 1., num_latents, name="log_alpha")
    return log_alpha


def decoder(b, num_latents, name, reuse):
    with tf.variable_scope(name, reuse=reuse):
        log_alpha = tf.layers.dense(2. * b - 1., num_latents, name="log_alpha")
    return log_alpha


def inference_network(x, mean, layer, num_layers, num_latents, name, reuse, sampler):
    with tf.variable_scope(name, reuse=reuse):
        log_alphas = []
        samples = []
        for l in range(num_layers):
            if l == 0:
                inp = ((x - mean) + 1.) / 2.
            else:
                inp = samples[-1]
            log_alpha = layer(inp, num_latents, layer_name(l), reuse)
            log_alphas.append(log_alpha)
            sample = sampler.sample(log_alpha, l)
            samples.append(sample)
    return log_alphas, samples


def layer_name(l):
    return "layer_{}".format(l)


def generator_network(samples, output_bias, layer, num_layers, num_latents, name, reuse, sampler=None, prior=None):
    with tf.variable_scope(name, reuse=reuse):
        log_alphas = []
        PRODUCE_SAMPLES = False
        if samples is None:
            PRODUCE_SAMPLES = True
            prior_log_alpha = prior
            samples = [None for l in range(num_layers)]
            samples[-1] = sampler.sample(prior_log_alpha, num_layers-1)
        for l in reversed(range(num_layers)):
            log_alpha = layer(
                samples[l],
                784 if l == 0 else num_latents, layer_name(l), reuse
            )
            if l == 0:
                log_alpha = log_alpha + output_bias
            log_alphas.append(log_alpha)
            if l > 0 and PRODUCE_SAMPLES:
                samples[l-1] = sampler.sample(log_alpha, l-1)
    return log_alphas


def Q_func(x, z, name, reuse):
    inp = tf.concat([x, z], 1)
    with tf.variable_scope(name, reuse=reuse):
        h1 = tf.layers.dense(inp, 200, tf.tanh, name="1")
        h2 = tf.layers.dense(h1, 200, tf.tanh, name="2")
        out = tf.layers.dense(h2, 1, name="out")
        #scale = tf.get_variable(
        #    "scale", shape=[1], dtype=tf.float32,
        #    initializer=tf.constant_initializer(0), trainable=True
        #)
    #return scale[0] * out
    return out
# def Q_func(z, name, reuse):
#     with tf.variable_scope(name, reuse=reuse):
#         h1 = tf.layers.dense(2. * z - 1., 200, tf.tanh, name="1")
#         h2 = tf.layers.dense(h1, 200, tf.tanh, name="2")
#         out = tf.layers.dense(h2, 1, name="out")
#         #scale = tf.get_variable(
#         #    "scale", shape=[1], dtype=tf.float32,
#         #    initializer=tf.constant_initializer(0), trainable=True
#         #)
#     #return scale[0] * out
#     return out

""" Variable Creation """
def create_log_temp(num):
    return tf.Variable(
        [np.log(.5) for i in range(num)],
        trainable=True,
        name='log_temperature',
        dtype=tf.float32
    )


def create_eta(num):
    return tf.Variable(
        [1.0 for i in range(num)],
        trainable=True,
        name='eta',
        dtype=tf.float32
    )


class BSampler:
    def __init__(self, u):
        self.u = u
    def sample(self, log_alpha, l):
        z = reparameterize(log_alpha, self.u[l])
        b = tf.to_float(tf.stop_gradient(z > 0))
        return b


class ZSampler:
    def __init__(self, u):
        self.u = u
    def sample(self, log_alpha, l):
        z = reparameterize(log_alpha, self.u[l])
        return z


class SIGZSampler:
    def __init__(self, u, temp):
        self.u = u
        self.temp = temp
    def sample(self, log_alpha, l):
        z = reparameterize(log_alpha, self.u[l])
        sig_z = concrete_relaxation(z, self.temp[l])
        return sig_z


def log_image(im_vec, name):
    # produce reconstruction summary
    a = tf.exp(im_vec)
    dec_log_theta = a / (1 + a)
    dec_log_theta_im = tf.reshape(dec_log_theta, [-1, 28, 28, 1])
    tf.summary.image(name, dec_log_theta_im)


def main(use_reinforce=False, relaxation=None, learn_prior=True, num_epochs=820,
         batch_size=24, num_latents=200, num_layers=2, lr=.0001, test_bias=False):
    TRAIN_DIR = "./binary_vae_time_test_relax"
    if os.path.exists(TRAIN_DIR):
        print("Deleting existing train dir")
        import shutil

        shutil.rmtree(TRAIN_DIR)
    os.makedirs(TRAIN_DIR)

    sess = tf.Session()
    dataset = input_data.read_data_sets("MNIST_data/", one_hot=True)
    train_binary = (dataset.train.images > .5).astype(np.float32)
    train_mean = np.mean(train_binary, axis=0, keepdims=True)
    train_output_bias = -np.log(1. / np.clip(train_mean, 0.001, 0.999) - 1.).astype(np.float32)

    x = tf.placeholder(tf.float32, [batch_size, 784])
    x_im = tf.reshape(x, [batch_size, 28, 28, 1])
    tf.summary.image("x_true", x_im)
    x_binary = tf.to_float(x > .5)

    # make prior for top b
    p_prior = tf.Variable(
        tf.zeros([num_latents],
        dtype=tf.float32),
        trainable=learn_prior,
        name='p_prior',
    )
    # create rebar specific variables temperature and eta
    log_temperatures = [create_log_temp(num_latents) for l in range(num_layers)]
    temperatures = [tf.exp(log_temp) for log_temp in log_temperatures]
    batch_temperatures = [tf.reshape(temp, [1, -1]) for temp in temperatures]
    etas = [create_eta(num_latents) for l in range(num_layers)]
    batch_etas = [tf.reshape(eta, [1, -1]) for eta in etas]

    # random uniform samples
    u = [
        tf.random_uniform([batch_size, num_latents], dtype=tf.float32)
        for l in range(num_layers)
    ]
    # create binary sampler
    b_sampler = BSampler(u)
    # generate hard forward pass
    encoder_name = "encoder"
    decoder_name = "decoder"
    inf_la_b, samples_b = inference_network(
        x_binary, train_mean,
        encoder, num_layers,
        num_latents, encoder_name, False, b_sampler
    )
    gen_la_b = generator_network(
        samples_b, train_output_bias,
        decoder, num_layers,
        num_latents, decoder_name, False
    )
    log_image(gen_la_b[-1], "x_pred")
    # produce samples
    _samples_la_b = generator_network(
        None, train_output_bias,
        decoder, num_layers,
        num_latents, decoder_name, True, sampler=b_sampler, prior=p_prior
    )
    log_image(_samples_la_b[-1], "x_sample")

    v = [v_from_u(_u, log_alpha) for _u, log_alpha in zip(u, inf_la_b)]
    # hard loss evaluation and log probs
    f_b, log_q_bs = neg_elbo(x_binary, samples_b, inf_la_b, gen_la_b, p_prior, log=True)
    total_loss = tf.reduce_mean(f_b)
    tf.summary.scalar("fb", total_loss)

    log_q_b = tf.add_n([tf.reduce_mean(log_q_b) for log_q_b in log_q_bs])
    if relaxation == "super":
        1/0
    else:
        # need to create soft samplers
        sig_z_sampler = SIGZSampler(u, batch_temperatures)
        sig_zt_sampler = SIGZSampler(v, batch_temperatures)
        # generate soft forward passes
        inf_la_z, samples_z = inference_network(
            x_binary, train_mean,
            encoder, num_layers,
            num_latents, encoder_name, True, sig_z_sampler
        )
        gen_la_z = generator_network(
            samples_z, train_output_bias,
            decoder, num_layers,
            num_latents, decoder_name, True
        )
        inf_la_zt, samples_zt = inference_network(
            x_binary, train_mean,
            encoder, num_layers,
            num_latents, encoder_name, True, sig_zt_sampler
        )
        gen_la_zt = generator_network(
            samples_zt, train_output_bias,
            decoder, num_layers,
            num_latents, decoder_name, True
        )
        f_z, _ = neg_elbo(x_binary, samples_z, inf_la_z, gen_la_z, p_prior)
        f_zt, _ = neg_elbo(x_binary, samples_zt, inf_la_zt, gen_la_zt, p_prior)
        tf.summary.scalar("fz", tf.reduce_mean(f_z))
        tf.summary.scalar("fzt", tf.reduce_mean(f_zt))
        if relaxation == "light":
            1/0

    # hard and soft loss gradients
    #d_f_b_d_la = [tf.gradients(f_b, la)[0] for la in inf_la_b]
    d_f_z_d_la = [tf.gradients(f_z, la)[0] for la in inf_la_z]
    d_f_zt_d_la = [tf.gradients(f_zt, la)[0] for la in inf_la_zt]
    # log-likelihood gradient
    d_log_q_d_la = [
        bernoulli_loglikelihood_derivitive(b, la)
        for b, la in zip(samples_b, inf_la_b)
    ]

    # create rebar and reinforce
    batch_f_b = tf.expand_dims(f_b, 1)
    batch_f_zt = tf.expand_dims(f_zt, 1)
    rebars = []
    reinforces = []
    variance_objectives = []
    encoder_gradvars = []
    model_opt = tf.train.AdamOptimizer(lr, beta2=.99999)
    for l in range(num_layers):
        # term1 = (batch_f_b - batch_etas[l] * batch_f_zt) * d_log_q_d_la[l]
        # term2 = batch_etas[l] * (d_f_z_d_la[l] - d_f_zt_d_la[l])
        # rebar = (term1 + term2) / batch_size
        # rebars.append(rebar)
        # reinforce = (batch_f_b * d_log_q_d_la[l]) / batch_size
        # reinforces.append(reinforce)
        # tf.summary.histogram("rebar_{}".format(l), rebar)
        # tf.summary.histogram("reinforce_{}".format(l), reinforce)
        # variance_objectives.append(tf.reduce_mean(tf.square(rebar)))
        #grads = tf.gradients(inf_la_b[l], layer_vars, grad_ys=(reinforce if use_reinforce else rebar))
        layer_vars = [v for v in tf.global_variables() if "encoder" in v.name and layer_name(l) in v.name]
        hard_f_gradvars = model_opt.compute_gradients(total_loss, var_list=layer_vars)
        for g, hfg, lv in zip(grads, hard_f_gradvars, layer_vars):
            print(gs(g), gs(hfg[0]), gs(lv))
            encoder_gradvars.append((g + hfg[0], lv))

    #tf.summary.histogram("rebar_sanity", rebars[0] - rebars[1])
    #tf.summary.histogram("reinforce_sanity", reinforces[0] - reinforces[1])

    variance_objective = tf.add_n(variance_objectives)
    variance_gradvars = model_opt.compute_gradients(variance_objective, var_list=log_temperatures+etas)
    decoder_vars = [v for v in tf.global_variables() if "decoder" in v.name]
    if learn_prior:
        decoder_vars = decoder_vars + [p_prior]
    decoder_gradvars = model_opt.compute_gradients(total_loss, var_list=decoder_vars)
    if use_reinforce:
        grad_vars = encoder_gradvars + decoder_gradvars
    else:
        grad_vars = encoder_gradvars + decoder_gradvars + variance_gradvars
    train_op = model_opt.apply_gradients(grad_vars)
    for g, v in grad_vars:
        print(g, v.name)
        tf.summary.histogram(v.name, v)
        tf.summary.histogram(v.name+"_grad", g)

    # encoder_params = [v for v in tf.global_variables() if "encoder" in v.name]
    # grads = {}
    # vals = [f_b, log_q_b]
    # names = ['f_b', 'log_q_b']
    # for val, name in zip(vals, names):
    #     val_gradvars = model_opt.compute_gradients(val, var_list=encoder_params)
    #     grads[name] = {}
    #     for g, v in val_gradvars:
    #         grads[name][v.name] = g
    #
    # grad_vars = []
    # etas = []
    # variance_objectives = []
    # q_objectives = []
    # rebars = []
    # reinforces = []
    # for l in range(num_layers):
    #     z = zs[l]
    #     zt = zts[l]
    #     name = "Q_{}".format(l)
    #     f_z_batch = Q_func(x_binary, z, name, False)
    #     f_zt_batch = Q_func(x_binary, zt, name, True)
    #     f_z = tf.reduce_mean(f_z_batch)
    #     f_zt = tf.reduce_mean(f_zt_batch)
    #     tf.summary.scalar("fz_{}".format(l), f_z)
    #     tf.summary.scalar("fzt_{}".format(l), f_zt)
    #     params = [v for v in encoder_params if "encoder/{}".format(l) in v.name]
    #     f_z_gradvars = model_opt.compute_gradients(f_z, var_list=params)
    #     f_zt_gradvars = model_opt.compute_gradients(f_zt, var_list=params)
    #     q_objectives.append(tf.reduce_mean(tf.square(f_b_batch - f_z_batch)) + tf.reduce_mean(tf.square(f_b_batch - f_zt_batch)))
    #     # sanity check to make sure same order
    #     for v1, v2 in zip(f_z_gradvars, f_zt_gradvars):
    #         assert v1[1] == v2[1], (v1[1], v2[1])
    #     for l, param in enumerate(params):
    #         print(param.name)
    #         d_fb_dt = grads['f_b'][param.name]
    #         d_fz_dt = f_z_gradvars[l][0]
    #         d_fzt_dt = f_zt_gradvars[l][0]
    #         d_log_q_dt = grads['log_q_b'][param.name]
    #
    #         # create eta
    #         eta = create_eta()
    #         tf.summary.scalar(eta.name, eta)
    #
    #         reinforce = f_b * d_log_q_dt + d_fb_dt
    #         rebar = (f_b - eta * f_zt) * d_log_q_dt + eta * (d_fz_dt - d_fzt_dt) + d_fb_dt
    #         tf.summary.histogram(param.name, param)
    #         tf.summary.histogram(param.name + "_reinforce", reinforce)
    #         tf.summary.histogram(param.name + "_rebar", rebar)
    #         if use_reinforce:
    #             grad_vars.append((reinforce, param))
    #         else:
    #             grad_vars.append((rebar, param))
    #         etas.append(eta)
    #         variance_objectives.append(tf.reduce_mean(tf.square(rebar)))
    #         rebars.append(rebar)
    #         reinforces.append(reinforce)
    #
    # decoder_params = [v for v in tf.global_variables() if "decoder" in v.name]
    # if learn_prior:
    #     decoder_params.append(p_prior)
    #
    # # for params in layer_params:
    # #     for param in params:
    # #     print(param.name)
    # #     # create eta
    # #     eta = create_eta()
    # #     tf.summary.scalar(eta.name, eta)
    # #
    # #     # # non reinforce gradient
    # #     d_fb_dt = grads['f_b'][param.name]
    # #     d_fz_dt = grads['f_z'][param.name]
    # #     d_fzt_dt = grads['f_zt'][param.name]
    # #     d_log_q_dt = grads['log_q_b'][param.name]
    # #
    # #     reinforce = f_b * d_log_q_dt + d_fb_dt
    # #     rebar = (f_b - eta * f_zt) * d_log_q_dt + eta * (d_fz_dt - d_fzt_dt) + d_fb_dt
    # #     tf.summary.histogram(param.name, param)
    # #     tf.summary.histogram(param.name+"_reinforce", reinforce)
    # #     tf.summary.histogram(param.name+"_rebar", rebar)
    # #     if use_reinforce:
    # #         grad_vars.append((reinforce, param))
    # #     else:
    # #         grad_vars.append((rebar, param))
    # #     etas.append(eta)
    # #     variance_objectives.append(tf.reduce_mean(tf.square(rebar)))
    # #     rebars.append(rebar)
    # #     reinforces.append(reinforce)
    #
    # decoder_gradvars = model_opt.compute_gradients(f_b, var_list=decoder_params)
    # for g, v in decoder_gradvars:
    #     print(v.name)
    #     tf.summary.histogram(v.name, v)
    #     tf.summary.histogram(v.name + "_grad", g)
    # grad_vars.extend(decoder_gradvars)
    #
    # variance_objective = tf.add_n(variance_objectives)
    # q_objective = tf.add_n(q_objectives) / 10.
    # model_train_op = model_opt.apply_gradients(grad_vars)
    # if use_reinforce:
    #     train_op = model_train_op
    # else:
    #     variance_opt = tf.train.AdamOptimizer(10. * lr, beta2=.99999)
    #     q_vars = [v for v in tf.trainable_variables() if "Q" in v.name]
    #     print("Q vars")
    #     for v in q_vars:
    #         print(v.name)
    #         tf.summary.histogram(v.name, v)
    #     variance_gradvars = variance_opt.compute_gradients(variance_objective, var_list=etas)
    #     q_gradvars = variance_opt.compute_gradients(q_objective, var_list=q_vars)
    #     variance_gradvars = variance_gradvars + q_gradvars
    #     for g, v in variance_gradvars:
    #         tf.summary.histogram(v.name+"_gradient", g)
    #     variance_train_op = variance_opt.apply_gradients(variance_gradvars)
    #     with tf.control_dependencies([model_train_op, variance_train_op]):
    #         train_op = tf.no_op()

    test_loss = tf.Variable(1000, trainable=False, name="test_loss", dtype=tf.float32)
    tf.summary.scalar("test_loss", test_loss)
    summ_op = tf.summary.merge_all()
    summary_writer = tf.summary.FileWriter(TRAIN_DIR)
    sess.run(tf.global_variables_initializer())

    iters_per_epoch = dataset.train.num_examples // batch_size
    iters = iters_per_epoch * num_epochs
    t = time.time()
    for i in range(iters):
        batch_xs, _ = dataset.train.next_batch(batch_size)
        if i % 100 == 0:
            loss, _, sum_str = sess.run([total_loss, train_op, summ_op], feed_dict={x: batch_xs})
            summary_writer.add_summary(sum_str, i)
            time_taken = time.time() - t
            t = time.time()
            print(i, loss, "{} / batch".format(time_taken / 100))
            if test_bias:
                rebs = []
                refs = []
                for _i in range(100000):
                    if _i % 1000 == 0:
                        print(_i)
                    rb, re = sess.run([rebars[3], reinforces[3]], feed_dict={x: batch_xs})
                    rebs.append(rb[:5])
                    refs.append(re[:5])
                rebs = np.array(rebs)
                refs = np.array(refs)
                re_var = np.log(refs.var(axis=0))
                rb_var = np.log(rebs.var(axis=0))
                print("rebar variance     = {}".format(rb_var))
                print("reinforce variance = {}".format(re_var))
                print("rebar     = {}".format(rebs.mean(axis=0)))
                print("reinforce = {}\n".format(refs.mean(axis=0)))
        else:
            loss, _ = sess.run([total_loss, train_op], feed_dict={x: batch_xs})

        if i % iters_per_epoch == 0:
            # epoch over, run test data
            losses = []
            for _ in range(dataset.test.num_examples // batch_size):
                batch_xs, _ = dataset.test.next_batch(batch_size)
                losses.append(sess.run(total_loss, feed_dict={x: batch_xs}))
            tl = np.mean(losses)
            print("Test loss = {}".format(tl))
            sess.run(test_loss.assign(tl))


if __name__ == "__main__":
    main(num_layers=1)
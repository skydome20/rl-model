from multi_env import MultiEnv
from env_runner import MultiEnvRunner
from policy_model import PolicyModel
import env_wrapper
import tensorflow as tf
import numpy as np
import os
import gym
import time
import argparse


#-------------------------
# Make an environment
#-------------------------
def make_env(rank, env_id="BipedalWalker-v2", rand_seed=0, unwrap=False):
	def _thunk():
		env = gym.make(env_id)
		if unwrap: env = env.unwrapped
		env.seed(rand_seed + rank)
		
		return env

	return _thunk


#Parse arguments
#----------------------------
parser = argparse.ArgumentParser()
parser.add_argument("--env", default="BipedalWalker-v2")
parser.add_argument("--render", action="store_true")
parser.add_argument("--unwrap", action="store_true")
args = parser.parse_args()


#Parameters
#----------------------------
n_env = 8
n_step = 16
mb_size = n_env*n_step
gamma = 0.99
ent_weight = 0.001
max_grad_norm=0.5
actor_lr = 3e-4
critic_lr = 3e-4
lr_decay = 0.99
eps = 1e-5
n_iter = 300000
disp_step = 100
save_step = 1000
is_render = args.render
env_id = args.env
save_dir = "./save_" + env_id


#Create multiple environments
#----------------------------
env = MultiEnv([make_env(i, env_id=env_id) for i in range(n_env)])
a_dim = env.ac_space.shape[0]
s_dim = env.ob_space.shape[0]
a_low = env.ac_space.low[0]
a_high = env.ac_space.high[0]
runner = MultiEnvRunner(env, s_dim, a_dim, n_step, gamma)


#Create the model
#----------------------------
config = tf.ConfigProto(
	intra_op_parallelism_threads=n_env,
	inter_op_parallelism_threads=n_env
)
config.gpu_options.allow_growth = True
sess = tf.Session(config=config)
policy = PolicyModel(sess, s_dim, a_dim, a_low, a_high, name="policy")


#Placeholders
#----------------------------
#action_ph: (mb_size, a_dim)
#adv_ph:    (mb_size)
#reward_ph: (mb_size)
action_ph = tf.placeholder(tf.float32, [None, a_dim], name="action")
adv_ph = tf.placeholder(tf.float32, [None], name="advantage")
discount_return_ph = tf.placeholder(tf.float32, [None], name="discounted_return")
actor_lr_ph = tf.placeholder(tf.float32, [])
critic_lr_ph = tf.placeholder(tf.float32, [])


#Loss
#----------------------------
neg_logprob = policy.distrib.neg_logp(action_ph)
pg_loss = tf.reduce_mean(neg_logprob * adv_ph)
ent = tf.reduce_mean(policy.distrib.entropy())
actor_loss = pg_loss - ent_weight*ent
critic_loss = tf.reduce_mean(tf.squared_difference(tf.squeeze(policy.value), discount_return_ph) / 2.0)


#Optimizer
#----------------------------
t_var = tf.trainable_variables()
actor_var = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope="policy/actor")
critic_var = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope="policy/critic")

actor_grads = tf.gradients(actor_loss, actor_var)
actor_grads, actor_grad_norm = tf.clip_by_global_norm(actor_grads, max_grad_norm)
actor_grads = list(zip(actor_grads, actor_var))
actor_opt = tf.train.RMSPropOptimizer(actor_lr_ph, decay=lr_decay, epsilon=eps).apply_gradients(actor_grads)

critic_grads = tf.gradients(critic_loss, critic_var)
critic_grads, critic_grad_norm = tf.clip_by_global_norm(critic_grads, max_grad_norm)
critic_grads = list(zip(critic_grads, critic_var))
critic_opt = tf.train.RMSPropOptimizer(critic_lr_ph, decay=lr_decay, epsilon=eps).apply_gradients(critic_grads)

tf.contrib.slim.model_analyzer.analyze_vars(t_var, print_info=True)


#Start training
#----------------------------
sess.run(tf.global_variables_initializer())

#Load the model
if not os.path.exists(save_dir):
	os.mkdir(save_dir)

saver = tf.train.Saver(var_list=t_var, max_to_keep=2)
ckpt = tf.train.get_checkpoint_state(save_dir)
if ckpt:
	print("Loading the model ... ", end="")
	global_step = int(ckpt.model_checkpoint_path.split("/")[-1].split("-")[-1])
	saver.restore(sess, ckpt.model_checkpoint_path)
	print("Done.")
else:
	global_step = 0

total_rewards = []
return_fp = open(os.path.join(save_dir, "avg_return.txt"), "a+")
t_start = time.time()

for it in range(global_step, n_iter+global_step+1):
	if is_render: env.render()

	#Train
	mb_obs, mb_actions, mb_values, mb_discount_returns = runner.run(policy)
	mb_advs = mb_discount_returns - mb_values

	cur_actor_loss, cur_critic_loss, cur_ent, _, _ = sess.run([actor_loss, critic_loss, ent, actor_opt, critic_opt], feed_dict={
		policy.ob_ph: mb_obs,
		action_ph: mb_actions,
		adv_ph: mb_advs,
		discount_return_ph: mb_discount_returns,
		actor_lr_ph: actor_lr,
		critic_lr_ph: critic_lr
	})

	#Show the result
	if it % disp_step == 0 and it > global_step:
		n_sec = time.time() - t_start
		fps = int((it-global_step)*n_env*n_step / n_sec)
		mean_total_reward, mean_len = runner.get_performance()
		total_rewards.append(mean_total_reward)

		print("[{:5d} / {:5d}]".format(it, n_iter+global_step))
		print("----------------------------------")
		print("Total timestep = {:d}".format(it * mb_size))
		print("Elapsed time = {:.2f} sec".format(n_sec))
		print("FPS = {:d}".format(fps))
		print("actor_loss = {:.6f}".format(cur_actor_loss))
		print("critic_loss = {:.6f}".format(cur_critic_loss))
		print("entropy = {:.6f}".format(cur_ent))
		print("mean_total_reward = {:.6f}".format(mean_total_reward))
		print("mean_len = {:.2f}".format(mean_len))
		print()

	#Save
	if it % save_step == 0 and it > global_step:
		print("Saving the model ... ", end="")
		saver.save(sess, save_dir+"/model.ckpt", global_step=it)

		for r in total_rewards:
			return_fp.write("{:f}\n".format(r))
		return_fp.flush()
		total_rewards = []
		print("Done.")
		print()

env.close()
return_fp.close()
print("Saving the model ... ", end="")
saver.save(sess, save_dir+"/model.ckpt", global_step=n_iter+global_step)
print("Done.")
print()
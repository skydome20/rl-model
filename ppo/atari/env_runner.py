import numpy as np
from collections import deque


#Runner for multiple environment
class MultiEnvRunner:
	#--------------------------
	# Constructor
	#--------------------------
	def __init__(self, env, img_height, img_width, c_dim, n_step=5, n_stack=4, gamma=0.99, lamb=0.95):
		self.env = env
		self.n_env = env.n_env
		self.n_step = n_step
		self.n_stack = n_stack
		self.gamma = gamma
		self.lamb = lamb
		self.img_height = img_height
		self.img_width = img_width
		self.c_dim = c_dim	

		#obs: (n_env, img_height, img_width, c_dim*n_stack)
		#dones: (n_env)
		self.stacked_obs = np.zeros((self.n_env, img_height, img_width, c_dim*n_stack), dtype=np.uint8)
		self.update_stacked_obs(self.env.reset())
		self.dones = [False for _ in range(self.n_env)]

		#Reward & length recorder
		self.total_rewards = np.zeros((self.n_env), dtype=np.float32)
		self.total_len = np.zeros((self.n_env), dtype=np.int32)
		self.reward_buf = deque(maxlen=100)
		self.len_buf = deque(maxlen=100)


	#--------------------------
	# Update stacked obs
	#--------------------------
	def update_stacked_obs(self, obs):
		#Shift 1 frame in the stack
		#Then put 1 new frame into the stack
		self.stacked_obs = np.roll(self.stacked_obs, shift=-self.c_dim, axis=3)
		self.stacked_obs[:, :, :, -self.c_dim:] = obs


	#--------------------------
	# Get a batch for n steps
	#--------------------------
	def run(self, policy):
		mb_obs = []
		mb_actions = [] 
		mb_values = []
		mb_rewards = []
		mb_dones = []
		mb_neg_logprobs = []

		#1. Run n steps
		#-------------------------------------
		for step in range(self.n_step):
			#obs:          (n_env, s_dim)
			#actions:      (n_env)
			#neg_logprobs: (n_env)
			#values:       (n_env)
			actions, values, neg_logprobs = policy.step(self.stacked_obs)
			mb_obs.append(np.copy(self.stacked_obs))
			mb_actions.append(actions)
			mb_values.append(values)
			mb_neg_logprobs.append(neg_logprobs)
			mb_dones.append(self.dones)

			#rewards: (n_env)
			#dones:   (n_env)
			obs, rewards, self.dones, infos = self.env.step(actions)
			mb_rewards.append(rewards)

			for i, done in enumerate(self.dones):
				if done: self.stacked_obs[i] = self.stacked_obs[i] * 0

			self.update_stacked_obs(obs)

		#last_values: (n_env)
		last_values = policy.value_step(self.stacked_obs)

		#2. Convert to np array
		#-------------------------------------
		#mb_obs:          (n_step, n_env, s_dim)
		#mb_actions:      (n_step, n_env)
		#mb_neg_logprobs: (n_step, n_env)
		#mb_values:       (n_step, n_env)
		#mb_rewards:      (n_step, n_env)
		#mb_dones:        (n_step, n_env)
		mb_obs = np.asarray(mb_obs, dtype=np.uint8)
		mb_actions = np.asarray(mb_actions, dtype=np.int32)
		mb_values = np.asarray(mb_values, dtype=np.float32)
		mb_rewards = np.asarray(mb_rewards, dtype=np.float32)
		mb_dones = np.asarray(mb_dones, dtype=np.bool)
		mb_neg_logprobs = np.asarray(mb_neg_logprobs, dtype=np.float32)

		self.record(mb_rewards, mb_dones)

		#3. Compute returns
		#-------------------------------------
		mb_returns = np.zeros_like(mb_rewards)
		mb_advs = np.zeros_like(mb_rewards)
		last_gae_lam = 0

		for t in reversed(range(self.n_step)):
			if t == self.n_step - 1:
				next_nonterminal = 1.0 - self.dones
				next_values = last_values
			else:
				next_nonterminal = 1.0 - mb_dones[t+1]
				next_values = mb_values[t+1]

			delta = mb_rewards[t] + self.gamma*next_values*next_nonterminal - mb_values[t]
			mb_advs[t] = last_gae_lam = delta + self.gamma*self.lamb*next_nonterminal*last_gae_lam

		mb_returns = mb_advs + mb_values

		#mb_obs:          (n_env*n_step, img_height, img_width, c_dim*n_stack)
		#mb_actions:      (n_env*n_step)
		#mb_neg_logprobs: (n_env*n_step)
		#mb_values:       (n_env*n_step)
		#mb_returns:      (n_env*n_step)
		return mb_obs.swapaxes(0, 1).reshape(self.n_env*self.n_step, self.img_height, self.img_width, self.c_dim*self.n_stack), \
				mb_actions.swapaxes(0, 1).flatten(), \
				mb_neg_logprobs.swapaxes(0, 1).flatten(), \
				mb_values.swapaxes(0, 1).flatten(), \
				mb_returns.swapaxes(0, 1).flatten()


	#--------------------------
	# Record reward & length
	#--------------------------
	def record(self, mb_rewards, mb_dones):
		for i in range(self.n_env):
			for j in range(self.n_step):
				if mb_dones[j, i] == True:
					self.reward_buf.append(self.total_rewards[i])
					self.len_buf.append(self.total_len[i])
					self.total_rewards[i] = mb_rewards[j, i]
					self.total_len[i] = 1
				else:
					self.total_rewards[i] += mb_rewards[j, i]
					self.total_len[i] += 1


	#--------------------------
	# Get performance
	#--------------------------
	def get_performance(self):
		if len(self.reward_buf) == 0:
			mean_total_reward = 0
		else:
			mean_total_reward = np.mean(self.reward_buf)

		if len(self.len_buf) == 0:
			mean_len = 0
		else:
			mean_len = np.mean(self.len_buf)

		return mean_total_reward, mean_len
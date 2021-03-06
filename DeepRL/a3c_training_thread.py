#!/usr/bin/env python3
import cv2
import logging
import numpy as np
import tensorflow as tf
import time
import matplotlib.pyplot as plt
import pickle
import pandas as pd
import os

from common.game_state import GameState
from common.game_state import get_wrapper_by_name
from common.util import generate_image_for_cam_video
from common.util import grad_cam
from common.util import make_movie
from common.util import transform_h
from common.util import transform_h_inv
from common.util import visualize_cam
from termcolor import colored
from queue import Queue
from copy import deepcopy
from common_worker import CommonWorker

logger = logging.getLogger("a3c_training_thread")

# Class for logging a TB row
# TODO: Make this a separate module, add useful functions to handle all the results plotting and such
class A3C_TB_RowLog():
    global_t = 0
    rewards = []
    batch_cumsum_rewards = []
    batch_raw_rewards = []
    # Display everything logged
    def show(self):
        print("Global Timstep: {}\nRewards: {}\nTransformed Cumulative Reward: {}\nRaw Cumulative Reward: {}\n\n"\
            .format(self.global_t, self.rewards, self.batch_cumsum_rewards, self.batch_raw_rewards))
    # Return everything in the class
    def getVals(self):
        return self.global_t, self.rewards, self.batch_cumsum_rewards, self.batch_raw_rewards

# Class for logging a "normal" A3C row
class A3C_RowLog():
    global_t = 0
    rewards = []
    batch_cumsum_rewards = []
    # Display everything logged
    def show(self):
        print("Global Timstep: {}\nRewards: {}\nCumulative Reward: {}\n\n"\
            .format(self.global_t, self.rewards, self.batch_cumsum_rewards))
    # Return everything in the class
    def getVals(self):
        return self.global_t, self.rewards, self.batch_cumsum_rewards

# Function that writes to the timestep-reward dictionary
# TODO: Make this a method of the RowLog class, that writes to a CSV and/or a dataframe
def dumpRowLog(row_log, pkl_file):
    try:
        os.makedirs(os.path.dirname(pkl_file), exist_ok=True)
        pickle.dump(row_log, open(pkl_file, "a+b"))
    except:
        print("\n\n\nSHOULD NOT SEE THIS\n\n\n")
        os.makedirs(os.path.dirname(pkl_file), exist_ok=True)
        pickle.dump(row_log, open(pkl_file, "wb"))

# Function that dumps the cumulative rewards to a pickle file (for Yumshu's exercise)
def logTrainRewards(reward, file_name):
    try:
        os.makedirs(os.path.dirname(file_name), exist_ok=True)
        pickle.dump(reward, open(file_name, "a+b"))
    except:
        os.makedirs(os.path.dirname(file_name), exist_ok=True)
        pickle.dump(reward, open(file_name, "wb"))


class A3CTrainingThread(CommonWorker):
    """Asynchronous Actor-Critic Training Thread Class."""
    log_interval = 100
    perf_log_interval = 1000
    local_t_max = 20
    entropy_beta = 0.01
    gamma = 0.99
    shaping_reward = 0.001
    shaping_factor = 1.
    shaping_gamma = 0.85
    advice_confidence = 0.8
    shaping_actions = -1  # -1 all actions, 0 exclude noop
    transformed_bellman = False
    clip_norm = 0.5
    use_grad_cam = False
    log_idx = 0
    reward_constant = 0


    def __init__(self, thread_index, global_net, local_net,
                 initial_learning_rate, learning_rate_input, grad_applier,
                 device=None, no_op_max=30):
        """Initialize A3CTrainingThread class."""
        assert self.action_size != -1

        self.thread_idx = thread_index
        self.learning_rate_input = learning_rate_input
        self.local_net = local_net

        self.no_op_max = no_op_max
        self.override_num_noops = 0 if self.no_op_max == 0 else None

        logger.info("===A3C thread_index: {}===".format(self.thread_idx))
        logger.info("device: {}".format(device))
        logger.info("local_t_max: {}".format(self.local_t_max))
        logger.info("action_size: {}".format(self.action_size))
        logger.info("entropy_beta: {}".format(self.entropy_beta))
        logger.info("gamma: {}".format(self.gamma))
        logger.info("reward_type: {}".format(self.reward_type))
        logger.info("transformed_bellman: {}".format(
            colored(self.transformed_bellman,
                    "green" if self.transformed_bellman else "red")))
        logger.info("clip_norm: {}".format(self.clip_norm))

        reward_clipped = True if self.reward_type == 'CLIP' else False
        local_vars = self.local_net.get_vars

        with tf.device(device):
            self.local_net.prepare_loss(
                entropy_beta=self.entropy_beta, critic_lr=0.5)
            var_refs = [v._ref() for v in local_vars()]

            self.gradients = tf.gradients(
                self.local_net.total_loss, var_refs)

        global_vars = global_net.get_vars

        with tf.device(device):
            if self.clip_norm is not None:
                self.gradients, grad_norm = tf.clip_by_global_norm(
                    self.gradients, self.clip_norm)
            self.gradients = list(zip(self.gradients, global_vars()))
            self.apply_gradients = grad_applier.apply_gradients(self.gradients)

        self.sync = self.local_net.sync_from(global_net)

        self.game_state = GameState(env_id=self.env_id, display=False,
                                    no_op_max=self.no_op_max, human_demo=False,
                                    episode_life=True,
                                    override_num_noops=self.override_num_noops)

        self.local_t = 0

        self.initial_learning_rate = initial_learning_rate

        self.episode_reward = 0
        self.transformed_episode_reward = 0
        self.episode_steps = 0

        # variable controlling log output
        self.prev_local_t = 0


    def train(self, sess, global_t, train_rewards):
        """Train A3C."""
        states = []
        actions = []
        rewards = []
        values = []
        rho = []

        # LOGGING ARRAYS
        if self.transformed_bellman:
            row_log = A3C_TB_RowLog()
        else:
            row_log = A3C_RowLog()

        terminal_pseudo = False  # loss of life
        terminal_end = False  # real terminal (lose all lives)

        # sync weights from glocal to local
        sess.run(self.sync)

        start_local_t = self.local_t

        # t_max times loop
        for i in range(self.local_t_max):
            state = cv2.resize(self.game_state.s_t,
                               self.local_net.in_shape[:-1],
                               interpolation=cv2.INTER_AREA)

            pi_, value_, logits_ = self.local_net.run_policy_and_value(sess,
                                                                       state)
            action = self.pick_action(logits_)

            states.append(state)
            actions.append(action)
            values.append(value_)

            # logging
            if self.thread_idx == self.log_idx \
               and self.local_t % self.log_interval == 0:
                log_msg1 = "lg={}".format(np.array_str(
                    logits_, precision=4, suppress_small=True))
                log_msg2 = "pi={}".format(np.array_str(
                    pi_, precision=4, suppress_small=True))
                log_msg3 = "V={:.4f}".format(value_)
                logger.debug(log_msg1)
                logger.debug(log_msg2)
                logger.debug(log_msg3)

            # process game
            self.game_state.step(action)

            # receive game result
            reward = self.game_state.reward
            terminal = self.game_state.terminal

            # Update rewards
            self.episode_reward += reward

            if self.reward_type == 'CLIP':
                reward = np.sign(reward)

            rewards.append(reward)

            self.local_t += 1
            self.episode_steps += 1
            global_t += 1

            # s_t1 -> s_t
            self.game_state.update()

            if terminal:
                terminal_pseudo = True

                env = self.game_state.env
                name = 'EpisodicLifeEnv'
                if get_wrapper_by_name(env, name).was_real_done:
                    log_msg = "train: worker={} global_t={} local_t={}".format(
                        self.thread_idx, global_t, self.local_t)
                    score_str = colored("score={}".format(
                        self.episode_reward), "magenta")
                    steps_str = colored("steps={}".format(
                        self.episode_steps), "blue")
                    log_msg += " {} {}".format(score_str, steps_str)
                    logger.debug(log_msg)
                    train_rewards['train'][global_t] = (self.episode_reward, self.episode_steps)
                    self.record_summary(
                        score=self.episode_reward, steps=self.episode_steps,
                        episodes=None, global_t=global_t, mode='Train')
                    self.episode_reward = 0
                    self.episode_steps = 0
                    terminal_end = True

                self.game_state.reset(hard_reset=False)
                break

        cumsum_reward = 0.0
        if not terminal:
            state = cv2.resize(self.game_state.s_t,
                               self.local_net.in_shape[:-1],
                               interpolation=cv2.INTER_AREA)
            cumsum_reward = self.local_net.run_value(sess, state)

        ###### ADD this code ######
        if self.transformed_bellman:
            raw_reward = cumsum_reward
            logTrainRewards(rewards, "results/TB_RewardLogs/MsPacman/tb_raw_rewards.pkl ")
            row_log.rewards = rewards
        else:
            logTrainRewards(rewards, "results/A3C_RewardLogs/MsPacman/a3c_clipped_rewards.pkl ")
            row_log.rewards = rewards
        ###### ADD this code ######

        actions.reverse()
        states.reverse()
        rewards.reverse()
        values.reverse()

        batch_state = []
        batch_action = []
        batch_adv = []
        batch_cumsum_reward = []
        ###### ADD this code ######
        if self.transformed_bellman:
            batch_raw_reward = []
        ###### ADD this code ######

        # compute and accumulate gradients
        # Cumulative reward computed here. That's the DISCOUNTED FUTURE REWARD
        for(ai, ri, si, vi) in zip(actions, rewards, states, values):
            if self.transformed_bellman:
                ri = np.sign(ri) * self.reward_constant + ri
                cumsum_reward = transform_h(ri + self.gamma * transform_h_inv(cumsum_reward))
                raw_reward = ri + self.gamma * raw_reward
            else:
                cumsum_reward = ri + self.gamma * cumsum_reward

            # Compute ADVANTAGE: Difference between value of state and (expected ?) cumulative reward
            advantage = cumsum_reward - vi

            # convert action to one-hot vector
            a = np.zeros([self.action_size])
            a[ai] = 1

            batch_state.append(si)
            batch_action.append(a)
            batch_adv.append(advantage)
            batch_cumsum_reward.append(cumsum_reward)
            if self.transformed_bellman:
                batch_raw_reward.append(raw_reward)
        
        # Log training raw rewards, and cumulative ones
        if self.transformed_bellman:
            logTrainRewards(batch_cumsum_reward, "results/TB_RewardLogs/MsPacman/tb_transformed_returns.pkl")
            logTrainRewards(batch_raw_reward, "results/TB_RewardLogs/MsPacman/raw_returns.pkl")
            row_log.batch_cumsum_rewards = batch_cumsum_reward
            row_log.batch_raw_rewards = batch_raw_reward
            row_log.global_t = global_t
            dumpRowLog(row_log, "results/RowLogs/MsPacman/TB_ROW-LOG.pkl")
            row_log.show()
        else:
            logTrainRewards(batch_cumsum_reward, "results/A3C_RewardLogs/MsPacman/a3c_clipped_returns.pkl")
            row_log.batch_cumsum_rewards = batch_cumsum_reward
            row_log.global_t = global_t
            dumpRowLog(row_log, "results/RowLogs/MsPacman/A3C_ROW-LOG.pkl")
            row_log.show()

        cur_learning_rate = self._anneal_learning_rate(global_t,
                self.initial_learning_rate )

        # perform A3C update
        feed_dict = {
            self.local_net.s: batch_state,
            self.local_net.a: batch_action,
            self.local_net.advantage: batch_adv,
            self.local_net.cumulative_reward: batch_cumsum_reward,
            self.learning_rate_input: cur_learning_rate,
            }

        sess.run(self.apply_gradients, feed_dict=feed_dict)

        t = self.local_t - self.prev_local_t
        if (self.thread_idx == self.log_idx and t >= self.perf_log_interval):
            self.prev_local_t += self.perf_log_interval
            elapsed_time = time.time() - self.start_time
            steps_per_sec = global_t / elapsed_time
            logger.info("worker-{}, log_worker-{}".format(self.thread_idx,
                                                          self.log_idx))
            logger.info("Performance : {} STEPS in {:.0f} sec. {:.0f}"
                        " STEPS/sec. {:.2f}M STEPS/hour".format(
                            global_t,  elapsed_time, steps_per_sec,
                            steps_per_sec * 3600 / 1000000.))

        # return advanced local step size
        diff_local_t = self.local_t - start_local_t
        return diff_local_t, terminal_end, terminal_pseudo

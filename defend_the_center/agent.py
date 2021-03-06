# coding: utf-8
# implement of agent
import time
import random
import pickle
import itertools as iter

import numpy as np
import tensorflow as tf


import matplotlib
gui_env = [i for i in matplotlib.rcsetup.interactive_bk]
for gui in gui_env:
    print("testing", gui)
    try:
        matplotlib.use(gui, warn=False, force=True)
        from matplotlib import pyplot as plt
        print("Using ..... ", matplotlib.get_backend())
    except:
        print("    ", gui, "Not found")

from vizdoom import *

import utils
import network


class Agent(object):
    """
    Agent
    """
    def __init__(self, game, name, s_size, a_size, optimizer=None, model_path=None, global_episodes=None, play=False):
        self.s_size = s_size
        self.a_size = a_size

        self.summary_step = 3

        self.name = "worker_" + str(name)
        self.number = name

        self.episode_reward = []
        self.episode_episode_health = []
        self.episode_lengths = []
        self.episode_mean_values = []
        self.episode_health = []
        self.episode_kills = []

        # Create the local copy of the network and the tensorflow op to
        # copy global parameters to local network
        if not play:
            self.model_path = model_path
            self.trainer = optimizer
            self.global_episodes = global_episodes
            self.increment = self.global_episodes.assign_add(1)
            self.local_AC_network = network.ACNetwork(self.name, optimizer, play=play)
            self.summary_writer = tf.summary.FileWriter("./summaries/defend_the_center/agent_%s" % str(self.number))
            self.update_local_ops = tf.group(*utils.update_target_graph('global', self.name))
        else:
            self.local_AC_network = network.ACNetwork(self.name, optimizer, play=play)
        if not isinstance(game, DoomGame):
            raise TypeError("Type Error")

        # The Below code is related to setting up the Doom environment
        game = DoomGame()
        # game.set_doom_scenario_path('../scenarios/deadly_corridor.cfg')
        game.load_config("../scenarios/defend_the_center.cfg")
        # game.set_doom_map("map01")
        game.set_screen_resolution(ScreenResolution.RES_640X480)
        game.set_screen_format(ScreenFormat.RGB24)
        game.set_render_hud(False)
        game.set_render_crosshair(False)
        game.set_render_weapon(True)
        game.set_render_decals(False)
        game.set_render_particles(False)
        # Enables labeling of the in game objects.
        game.set_labels_buffer_enabled(True)
        game.add_available_button(Button.TURN_LEFT)
        game.add_available_button(Button.TURN_RIGHT)
        game.add_available_button(Button.ATTACK)
        game.add_available_game_variable(GameVariable.USER1)
        game.set_episode_timeout(2100)
        game.set_episode_start_time(5)
        game.set_window_visible(play)
        game.set_sound_enabled(False)
        game.set_living_reward(0)
        game.set_mode(Mode.PLAYER)
        if play:
            # game.add_game_args("+viz_render_all 1")
            game.set_render_hud(False)
            game.set_ticrate(35)
        game.init()
        self.env = game
        self.actions = self.button_combinations()

    def infer(self, rollout, sess, gamma, bootstrap_value):
        rollout = np.array(rollout)
        observations = rollout[:, 0]
        actions = rollout[:, 1]
        rewards = rollout[:, 2]
        next_observations = rollout[:, 3]
        values = rollout[:, 5]

        # Here we take the rewards and values from the rollout, and use them to
        # generate the advantage and discounted returns.
        # The advantage function uses "Generalized Advantage Estimation"
        self.rewards_plus = np.asarray(rewards.tolist() + [bootstrap_value])
        discounted_rewards = utils.discount(self.rewards_plus, gamma)[:-1]
        self.value_plus = np.asarray(values.tolist() + [bootstrap_value])
        advantages = rewards + gamma * self.value_plus[1:] - self.value_plus[:-1]
        advantages = utils.discount(advantages, gamma)

        # Update the global network using gradients from loss
        # Generate network statistics to periodically save
        feed_dict = {
            self.local_AC_network.target_v: discounted_rewards,
            self.local_AC_network.inputs: np.stack(observations),
            self.local_AC_network.actions: actions,
            self.local_AC_network.advantages: advantages
        }
        l, v_l, p_l, e_l, g_n, v_n, _ = sess.run([
                                            self.local_AC_network.loss,
                                            self.local_AC_network.value_loss,
                                            self.local_AC_network.policy_loss,
                                            self.local_AC_network.entropy,
                                            self.local_AC_network.grad_norms,
                                            self.local_AC_network.var_norms,
                                            self.local_AC_network.apply_grads],
                                            feed_dict=feed_dict)
        return l / len(rollout), v_l / len(rollout), p_l / len(rollout), e_l / len(rollout), g_n, v_n

    def train_a3c(self, max_episode_length, gamma, sess, coord, saver):
        if not isinstance(saver, tf.train.Saver):
            raise TypeError('saver should be tf.train.Saver')

        episode_count = sess.run(self.global_episodes)
        start_t = time.time()
        print("Starting worker " + str(self.number))
        with sess.as_default(), sess.graph.as_default():
            while not coord.should_stop():
                sess.run(self.update_local_ops)  # update local ops in every episode
                episode_buffer = []
                episode_values = []
                episode_reward = 0
                episode_kills = 0
                episode_step_count = 0
                d = False

                last_total_health = 100
                last_total_ammo2 = 26  # total is 26

                self.env.new_episode()
                episode_st = time.time()
                while not self.env.is_episode_finished():

                    # if utils.check_play(self.env.get_state()):
                    s = self.env.get_state().screen_buffer
                    s = utils.process_frame(s)
                    # Take an action using probabilities from policy network output.
                    a_dist, v = sess.run([self.local_AC_network.policy, self.local_AC_network.value],
                                         feed_dict={self.local_AC_network.inputs: [s]})
                    # get a action_index from a_dist in self.local_AC.policy
                    a_index = self.choose_action_index(a_dist[0], deterministic=False)
                    # make an action
                    shoot_reward = self.env.make_action(self.actions[a_index], 4)

                    ammo2_delta = self.env.get_game_variable(GameVariable.AMMO2) - last_total_ammo2
                    last_total_ammo2 = self.env.get_game_variable(GameVariable.AMMO2)

                    health_delta = self.env.get_game_variable(GameVariable.HEALTH) - last_total_health
                    last_total_health = self.env.get_game_variable(GameVariable.HEALTH)

                    health_reward = self.health_reward_function(health_delta)
                    ammo2_reward = self.ammo2_reward_function(ammo2_delta)

                    reward = shoot_reward + health_reward + ammo2_reward
                    episode_reward += reward
                    episode_kills += shoot_reward

                    d = self.env.is_episode_finished()
                    if d:
                        s1 = s
                    else:  # game is not finished
                        s1 = self.env.get_state().screen_buffer
                        s1 = utils.process_frame(s1)

                    episode_buffer.append([s, a_index, reward, s1, d, v[0, 0]])
                    episode_values.append(v[0, 0])
                    # summaries information
                    s = s1
                    episode_step_count += 1

                    # If the episode hasn't ended, but the experience buffer is full, then we
                    # make an update step using that experience rollout.
                    if len(episode_buffer) == 32 and d is False and episode_step_count != max_episode_length - 1:
                        # Since we don't know what the true final return is,
                        # we "bootstrap" from our current value estimation.
                        v1 = sess.run(self.local_AC_network.value, feed_dict={self.local_AC_network.inputs: [s]})[0, 0]
                        l, v_l, p_l, e_l, g_n, v_n = self.infer(episode_buffer, sess, gamma, v1)
                        episode_buffer = []
                        sess.run(self.update_local_ops)
                    if d is True or last_total_ammo2 <= 0:
                        self.episode_health.append(self.env.get_game_variable(GameVariable.HEALTH))
                        print('{}, health: {}, episode #{}, reward: {}, killed:{}, ammo2_left:{}, time costs:{}'.format(
                            self.name, last_total_health, episode_count,
                            episode_reward, episode_kills, last_total_ammo2, time.time()-episode_st))
                        break

                # summaries
                self.episode_reward.append(episode_reward)
                self.episode_episode_health.append(last_total_health)
                self.episode_lengths.append(episode_step_count)
                self.episode_mean_values.append(np.mean(episode_values))
                self.episode_kills.append(episode_kills)
                # Update the network using the experience buffer at the end of the episode.
                if len(episode_buffer) != 0:
                    l, v_l, p_l, e_l, g_n, v_n = self.infer(episode_buffer, sess, gamma, 0.0)

                # Periodically save gifs of episodes, model parameters, and summary statistics.
                if episode_count % 5 == 0 and episode_count != 0:
                    if episode_count % 50 == 0 and self.name == 'worker_0':
                        saver.save(sess, self.model_path+'/model-'+str(episode_count)+'.ckpt')
                        print("Episode count {}, saved Model, time costs {}".format(episode_count, time.time()-start_t))
                        start_t = time.time()

                    mean_picked = np.mean(self.episode_episode_health[-5:])
                    mean_reward = np.mean(self.episode_reward[-5:])
                    mean_health = np.mean(self.episode_health[-5:])
                    mean_length = np.mean(self.episode_lengths[-5:])
                    mean_value = np.mean(self.episode_mean_values[-5:])
                    mean_kills = np.mean(self.episode_kills[-5:])
                    summary = tf.Summary()
                    summary.value.add(tag='Performance/Reward', simple_value=mean_reward)
                    summary.value.add(tag='Performance/Kills', simple_value=mean_kills)
                    summary.value.add(tag='Performance/Health', simple_value=mean_health)
                    summary.value.add(tag='Performance/Value', simple_value=mean_value)
                    summary.value.add(tag='Losses/Total Loss', simple_value=l)
                    summary.value.add(tag='Losses/Value Loss', simple_value=v_l)
                    summary.value.add(tag='Losses/Policy Loss', simple_value=p_l)
                    summary.value.add(tag='Losses/Entropy', simple_value=e_l)
                    summary.value.add(tag='Losses/Grad Norm', simple_value=g_n)
                    summary.value.add(tag='Losses/Var Norm', simple_value=v_n)
                    self.summary_writer.add_summary(summary, episode_count)
                    self.summary_writer.flush()

                if self.name == 'worker_0':
                    sess.run(self.increment)
                episode_count += 1
                if episode_count == 120000:  # thread to stop
                    print("Stop training name:{}".format(self.name))
                    coord.request_stop()

    def play_game(self, sess, episode_num):
        if not isinstance(sess, tf.Session):
            raise TypeError('saver should be tf.train.Saver')

        for i in range(episode_num):

            self.env.new_episode()
            state = self.env.get_state()
            s = utils.process_frame(state.screen_buffer)
            episode_rewards = 0
            last_total_shaping_reward = 0
            step = 0
            s_t = time.time()
            while not self.env.is_episode_finished():
                state = self.env.get_state()
                s = utils.process_frame(state.screen_buffer)
                a_dist, v = sess.run([self.local_AC_network.policy, self.local_AC_network.value],
                                     feed_dict={self.local_AC_network.inputs: [s]})
                # get a action_index from a_dist in self.local_AC.policy
                a_index = self.choose_action_index(a_dist[0], deterministic=True)
                # make an action
                reward = self.env.make_action(self.actions[a_index])
                step += 1
                episode_rewards += reward

                print('Current step: #{}'.format(step))
                print('Current action: ', self.actions[a_index])
                print('Current health: ', self.env.get_game_variable(GameVariable.HEALTH))
                print('Current amm02: {0}'.format(self.env.get_game_variable(GameVariable.AMMO2)))
                print('Current reward: {0}'.format(reward))
                if self.env.get_game_variable(GameVariable.AMMO2) <= 0:
                    break
            print("----------------")
            print('Run out of AMMO')
            print('End episode: {}, Total Reward: {}'.format(i, episode_rewards))
            print('time costs: {}'.format(time.time() - s_t))
            time.sleep(5)

    @staticmethod
    def choose_action_index(policy, deterministic=False):
        if deterministic:
            return np.argmax(policy)

        r = random.random()
        cumulative_reward = 0
        for i, p in enumerate(policy):
            cumulative_reward += p
            if r <= cumulative_reward:
                return i

        return len(policy) - 1

    def health_reward_function(self, health_delta):
        health, reward = self.env.get_game_variable(GameVariable.HEALTH), 0
        if health_delta == 0:
            return 0
        elif health_delta < 0:
            reward = -0.5
        return reward

    @staticmethod
    def ammo2_reward_function(ammo2_delta):
        if ammo2_delta == 0:
            return 0
        elif ammo2_delta > 0:
            return -0.05
        else:
            return -0.05

    def button_combinations(self):
        actions = [list(perm) for perm in iter.product([False, True], repeat=self.env.get_available_buttons_size())]
        actions.remove([True, True, False])
        actions.remove([True, True, True])
        return actions

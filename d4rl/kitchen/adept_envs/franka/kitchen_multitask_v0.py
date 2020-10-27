""" Kitchen environment for long horizon manipulation """
#!/usr/bin/python
#
# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import copy
import os

import mujoco_py
import numpy as np
from d4rl.kitchen.adept_envs import robot_env
from d4rl.kitchen.adept_envs.utils.configurable import configurable
from gym import spaces
from dm_control.mujoco import engine

from robosuite.controllers import (
    EndEffectorImpedanceController,
    EndEffectorInverseKinematicsController,
)


@configurable(pickleable=True)
class KitchenV0(robot_env.RobotEnv):

    CALIBRATION_PATHS = {
        "default": os.path.join(os.path.dirname(__file__), "robot/franka_config.xml")
    }
    # Converted to velocity actuation
    ROBOTS = {"robot": "d4rl.kitchen.adept_envs.franka.robot.franka_robot:Robot_VelAct"}
    MODEl = os.path.join(
        os.path.dirname(__file__), "../franka/assets/franka_kitchen_jntpos_act_ab.xml"
    )
    N_DOF_ROBOT = 9
    N_DOF_OBJECT = 21

    def __init__(self, robot_params={}, frame_skip=40):
        self.goal_concat = True
        self.obs_dict = {}
        self.robot_noise_ratio = 0.1  # 10% as per robot_config specs
        self.goal = np.zeros((30,))
        super().__init__(
            self.MODEl,
            robot=self.make_robot(
                n_jnt=self.N_DOF_ROBOT,  # root+robot_jnts
                n_obj=self.N_DOF_OBJECT,
                **robot_params
            ),
            frame_skip=frame_skip,
            camera_settings=dict(
                distance=2.2, lookat=[-0.2, 0.5, 2.0], azimuth=70, elevation=-35
            ),
        )
        self.reset_mocap_welds(self.sim)
        self.sim.forward()

        gripper_target = np.array(
            [-0.498, 0.005, -0.431 + 0.01]
        ) + self.sim.data.get_site_xpos("end_effector")
        gripper_rotation = np.array([1.0, 0.0, 1.0, 0.0])
        self.sim.data.set_mocap_pos("mocap", gripper_target)
        self.sim.data.set_mocap_quat("mocap", gripper_rotation)
        for _ in range(10):
            self.sim.step()

        self.init_qpos = self.sim.model.key_qpos[0].copy()
        # For the microwave kettle slide hinge
        self.init_qpos = np.array(
            [
                1.48388023e-01,
                -1.76848573e00,
                1.84390296e00,
                -2.47685760e00,
                2.60252026e-01,
                7.12533105e-01,
                1.59515394e00,
                4.79267505e-02,
                3.71350919e-02,
                -2.66279850e-04,
                -5.18043486e-05,
                3.12877220e-05,
                -4.51199853e-05,
                -3.90842156e-06,
                -4.22629655e-05,
                6.28065475e-05,
                4.04984708e-05,
                4.62730939e-04,
                -2.26906415e-04,
                -4.65501369e-04,
                -6.44129196e-03,
                -1.77048263e-03,
                1.08009684e-03,
                -2.69397440e-01,
                3.50383255e-01,
                1.61944683e00,
                1.00618764e00,
                4.06395120e-03,
                -6.62095997e-03,
                -2.68278933e-04,
            ]
        )

        self.init_qvel = self.sim.model.key_qvel[0].copy()

        self.act_mid = np.zeros(self.N_DOF_ROBOT)
        self.act_amp = 2.0 * np.ones(self.N_DOF_ROBOT)

        act_lower = -1 * np.ones((self.N_DOF_ROBOT,))
        act_upper = 1 * np.ones((self.N_DOF_ROBOT,))
        self.action_space = spaces.Box(act_lower, act_upper)

        obs_upper = 8.0 * np.ones(self.obs_dim)
        obs_lower = -obs_upper
        self.observation_space = spaces.Box(obs_lower, obs_upper)

    def ctrl_set_action(self, sim, action):
        """For torque actuators it copies the action into mujoco ctrl field.
        For position actuators it sets the target relative to the current qpos.
        """
        if sim.model.nmocap > 0:
            _, action = np.split(action, (sim.model.nmocap * 7,))
        if sim.data.ctrl is not None:
            for i in range(action.shape[0]):
                if sim.model.actuator_biastype[i] == 0:
                    sim.data.ctrl[i] = action[i]
                else:
                    idx = sim.model.jnt_qposadr[sim.model.actuator_trnid[i, 0]]
                    sim.data.ctrl[i] = sim.data.qpos[idx] + action[i]

    def mocap_set_action(self, sim, action):
        """The action controls the robot using mocaps. Specifically, bodies
        on the robot (for example the gripper wrist) is controlled with
        mocap bodies. In this case the action is the desired difference
        in position and orientation (quaternion), in world coordinates,
        of the of the target body. The mocap is positioned relative to
        the target body according to the delta, and the MuJoCo equality
        constraint optimizer tries to center the welded body on the mocap.
        """
        if sim.model.nmocap > 0:
            action, _ = np.split(action, (sim.model.nmocap * 7,))
            action = action.reshape(sim.model.nmocap, 7)

            pos_delta = action[:, :3]
            quat_delta = action[:, 3:]

            self.reset_mocap2body_xpos(sim)
            sim.data.mocap_pos[:] = sim.data.mocap_pos + pos_delta
            sim.data.mocap_quat[:] = sim.data.mocap_quat

    def reset_mocap_welds(self, sim):
        """Resets the mocap welds that we use for actuation."""
        if sim.model.nmocap > 0 and sim.model.eq_data is not None:
            for i in range(sim.model.eq_data.shape[0]):
                if sim.model.eq_type[i] == mujoco_py.const.EQ_WELD:
                    sim.model.eq_data[i, :] = np.array(
                        [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
                    )
        sim.forward()

    def reset_mocap2body_xpos(self, sim):
        """Resets the position and orientation of the mocap bodies to the same
        values as the bodies they're welded to.
        """

        if (
            sim.model.eq_type is None
            or sim.model.eq_obj1id is None
            or sim.model.eq_obj2id is None
        ):
            return
        for eq_type, obj1_id, obj2_id in zip(
            sim.model.eq_type, sim.model.eq_obj1id, sim.model.eq_obj2id
        ):
            if eq_type != mujoco_py.const.EQ_WELD:
                continue

            mocap_id = sim.model.body_mocapid[obj1_id]
            if mocap_id != -1:
                # obj1 is the mocap, obj2 is the welded body
                body_idx = obj2_id
            else:
                # obj2 is the mocap, obj1 is the welded body
                mocap_id = sim.model.body_mocapid[obj2_id]
                body_idx = obj1_id

            assert mocap_id != -1
            sim.data.mocap_pos[mocap_id][:] = sim.data.body_xpos[body_idx]
            sim.data.mocap_quat[mocap_id][:] = sim.data.body_xquat[body_idx]

    def _set_action(self, action):
        assert action.shape == (4,)
        action = (
            action.copy()
        )  # ensure that we don't change the action outside of this scope
        pos_ctrl, gripper_ctrl = action[:3], action[3]

        pos_ctrl *= 0.05  # limit maximum change in position
        rot_ctrl = [
            1.0,
            0.0,
            1.0,
            0.0,
        ]  # fixed rotation of the end effector, expressed as a quaternion
        gripper_ctrl = np.array([gripper_ctrl, gripper_ctrl])
        assert gripper_ctrl.shape == (2,)
        action = np.concatenate([pos_ctrl, rot_ctrl, gripper_ctrl])

        # Apply action to simulation.
        self.ctrl_set_action(self.sim, action)
        self.mocap_set_action(self.sim, action)

    def _get_reward_n_score(self, obs_dict):
        raise NotImplementedError()

    def grasp(self):
        for i in range(self.skip):
            self.sim.data.qpos[7] -= 0.0003
            self.sim.data.qpos[8] -= 0.0003
            self.sim.step()

    def rotate_ee(self):
        for i in range(self.skip):
            self.sim.data.qpos[6] -= 0.01
            self.sim.step()

    def goto_pose(self, pose):
        for i in range(self.skip):
            self.controller.set_goal(pose)

    def step(self, a, b=None):
        a = np.clip(a, -1.0, 1.0)

        # action = np.clip(a, self.action_space.low, self.action_space.high)

        if not self.initializing:
            a = self.act_mid + a * self.act_amp  # mean center and scale
        else:
            self.goal = self._get_task_goal()  # update goal if init
            self.controller = EndEffectorImpedanceController(
                self.sim,
                eef_name="panda0_link7",
                joint_indexes={
                    "joints": list(range(9)),
                    "qpos": list(range(9)),
                    "qvel": list(range(9)),
                },
                control_ori=False,
                policy_freq=self.skip,
                control_delta=True,
            )
            # self.controller = EndEffectorInverseKinematicsController(
            #     self.sim,
            #     eef_name="panda0_link7",
            #     robot_name="panda",
            #     actuator_range=self.sim.model.actuator_ctrlrange,
            #     joint_indexes={
            #         "joints": list(range(9)),
            #         "qpos": list(range(9)),
            #         "qvel": list(range(9)),
            #     },
            #     ik_ori_limit=1,
            #     ik_pos_limit=1,
            # )
        # a[:3] = [0, 0, 0.0]
        # a[3:7] = [0, 0, 0, 0]
        # self.controller.set_goal(a[:3])
        # for i in range(self.skip):
        #     a = self.controller.run_controller()
        #     self.do_simulation(a, 1)
        #
        # for i in range(self.skip):
        #     self.robot.step(self, self.controller, a, step_duration=1)
        # self.sim.data.qpos[7] -= 0.001
        # self.sim.data.qpos[6] -= 1
        # self.sim.data.qpos[5] -= 1
        # self.sim.step()
        # self.grasp()
        # print(self.get_endeff_pos())

        # print(self.get_endeff_pos())
        # observations
        a = np.array([0, 1, 0, 0]).astype(float)
        self._set_action(a)
        self.sim.step()
        obs = self._get_obs()

        # rewards
        reward_dict, score = self._get_reward_n_score(self.obs_dict)

        # termination
        done = False

        # finalize step
        env_info = {
            "time": self.obs_dict["t"],
            "score": score,
        }
        return obs, reward_dict["r_total"], done, env_info

    def _get_obs(self):
        t, qp, qv, obj_qp, obj_qv = self.robot.get_obs(
            self, robot_noise_ratio=self.robot_noise_ratio
        )

        self.obs_dict = {}
        self.obs_dict["t"] = t
        self.obs_dict["qp"] = qp
        self.obs_dict["qv"] = qv
        self.obs_dict["obj_qp"] = obj_qp
        self.obs_dict["obj_qv"] = obj_qv
        self.obs_dict["goal"] = self.goal
        if self.goal_concat:
            return np.concatenate(
                [self.obs_dict["qp"], self.obs_dict["obj_qp"], self.obs_dict["goal"]]
            )

    def reset_model(self):
        reset_pos = self.init_qpos[:].copy()
        reset_vel = self.init_qvel[:].copy()
        self.robot.reset(self, reset_pos, reset_vel)
        self.sim.forward()
        self.goal = self._get_task_goal()  # sample a new goal on reset
        return self._get_obs()

    def evaluate_success(self, paths):
        # score
        mean_score_per_rollout = np.zeros(shape=len(paths))
        for idx, path in enumerate(paths):
            mean_score_per_rollout[idx] = np.mean(path["env_infos"]["score"])
        mean_score = np.mean(mean_score_per_rollout)

        # success percentage
        num_success = 0
        num_paths = len(paths)
        for path in paths:
            num_success += bool(path["env_infos"]["rewards"]["bonus"][-1])
        success_percentage = num_success * 100.0 / num_paths

        # fuse results
        return np.sign(mean_score) * (
            1e6 * round(success_percentage, 2) + abs(mean_score)
        )

    def close_env(self):
        self.robot.close()

    def set_goal(self, goal):
        self.goal = goal

    def _get_task_goal(self):
        return self.goal

    # Only include goal
    @property
    def goal_space(self):
        len_obs = self.observation_space.low.shape[0]
        env_lim = np.abs(self.observation_space.low[0])
        return spaces.Box(low=-env_lim, high=env_lim, shape=(len_obs // 2,))

    def convert_to_active_observation(self, observation):
        return observation


class KitchenTaskRelaxV1(KitchenV0):
    """Kitchen environment with proper camera and goal setup"""

    def __init__(self):
        super(KitchenTaskRelaxV1, self).__init__()

    def _get_reward_n_score(self, obs_dict):
        reward_dict = {}
        reward_dict["true_reward"] = 0.0
        reward_dict["bonus"] = 0.0
        reward_dict["r_total"] = 0.0
        score = 0.0
        return reward_dict, score

    def render(self, mode="human"):
        if mode == "rgb_array":
            # camera = engine.MovableCamera(self.sim, 256, 256)
            # camera.set_pose(
            #     distance=2.2, lookat=[-0.2, 0.5, 2.0], azimuth=70, elevation=-35
            # )
            # img = camera.render()
            img = self.sim_robot.renderer.render_offscreen(1000, 1000)
            return img
        else:
            super(KitchenTaskRelaxV1, self).render()

#!/usr/bin/env python

import rospy
import numpy.linalg as LA
import time

#----- ROS Msgs ------

from nav_msgs.msg import Odometry                                                # used to recieve info from the base
from geometry_msgs.msg import Twist                                              # used to set comand for the base

from door_opening_on_real_robot_demo.msg import *

from door_opening_on_real_robot_demo.srv import *

from franka_msgs.srv import *

from door_opening_on_real_robot_demo.ROS_door_opening_util import *

#----- Other -----

from scipy.spatial.transform import Rotation as R

import pybullet as p
import pybullet_data

import numpy as np
import math

#----- Description -----

# This is the main class that utilizes the information from the online direction
# estimtaion module and the velocity planner module in order to perform a complete
# loop iteration when called from the state machine. It is designed such that it
# very much resembles the one provided for the Gazebo simulation.

#-----------------------

class RobotPlanner:

    def __init__(self, controller, direction_estimatior, controller_init):

        self.controller = controller
        self.direction_estimator = direction_estimatior

        self.cinit = controller_init

        self.processing = False

        #----- Msg buffer -----

        self.baseState_msg = None                                                # Receives the Position and Velocity of the base

        #----- Check if required topics started publishing -----

        self.base_state_topic_initiated = False

        self.start_optimization = False

        #----- Robot state -----

        self.M = None
        self.b = None
        self.q = None
        self.q_dot = None
        self.tau = None
        self.J_b_ee = None
        self.T_b_ee = None
        self.force = None
        self.torque = None

        self.linVelBase = None                                                   # should be a list of 3 elements
        self.angVelBase = None                                                   # should be a list of 3 elements
        self.T_O_b = None

        #----- Subsrcribers and services -----

        self.panda_model_state_srv = rospy.ServiceProxy('/panda_state_srv', PandaStateSrv)
        self.robot_gripper_srv = rospy.ServiceProxy('/robot_gripper_srv', PandaGripperSrv)

        self.panda_EE_frame_srv = rospy.ServiceProxy('/franka_control/set_EE_frame', SetEEFrame)
        self.panda_K_frame_srv = rospy.ServiceProxy('/franka_control/set_K_frame', SetKFrame)
        self.panda_set_collision_behaviour = rospy.ServiceProxy('/franka_control/set_force_torque_collision_behavior', SetForceTorqueCollisionBehavior)

        self.subscriber_base_state = rospy.Subscriber('/ridgeback_velocity_controller/odom', Odometry , self.baseState_cb)

        #----- Publishers -----

        self.publisher_joints = rospy.Publisher('/arm_command', desired_vel_msg, latch=True, queue_size=10)
        self.publisher_base_velocity = rospy.Publisher('/cmd_vel', Twist,  latch=True, queue_size=10)

        #----- True Init Direction -----

        self.true_init_dir = [0.0, 0.0, 0.0]
        self.world_ee_pos = []

#-------
    def baseState_cb(self, msg):

        if not self.processing:

            self.baseState_msg = msg
            if not self.base_state_topic_initiated:
                self.base_state_topic_initiated = True

#-------
    def publishArmAndBaseVelocityControl(self, q_dot_des, linVelBase, angVelBase):

        joints_des = desired_vel_msg()

        for i in range(7):
            joints_des.dq_arm[i] = q_dot_des[i]

        base_des = Twist()
        base_des.linear.x = linVelBase[0]
        base_des.linear.y = linVelBase[1]
        base_des.linear.z = 0.0

        base_des.angular.x = 0.0
        base_des.angular.y = 0.0
        base_des.angular.z = angVelBase

        self.publisher_base_velocity.publish(base_des)
        self.publisher_joints.publish(joints_des)

#-------
    def set_frames(self, F_T_EE, EE_T_K):

        EE_frame_req = SetEEFrameRequest()
        K_frame_req = SetKFrameRequest()

        for i in range(16):

            EE_frame_req.F_T_EE[i] = F_T_EE[i]
            K_frame_req.EE_T_K[i] = EE_T_K[i]

        print("Sending EE_frame request...")

        res1 = self.panda_EE_frame_srv(EE_frame_req)

        print("Received: "+str(res1.success))

        print("Sending K_frame request...")

        res2 = self.panda_K_frame_srv(K_frame_req)

        print("Received: "+str(res2.success))

        if res1.success and res2.success:
            return True

        else:
            return False

#-------
    def set_force_torque_collision(self, lower_torque, higher_torque, lower_force, higher_force):

        SetForceTorqueCollisionBehavior_req = SetForceTorqueCollisionBehaviorRequest()

        for i in range(7):
            SetForceTorqueCollisionBehavior_req.lower_torque_thresholds_nominal[i] = lower_torque[i]
            SetForceTorqueCollisionBehavior_req.upper_torque_thresholds_nominal[i] = higher_torque[i]

        for i in range(6):
            SetForceTorqueCollisionBehavior_req.lower_force_thresholds_nominal[i] = lower_force[i]
            SetForceTorqueCollisionBehavior_req.upper_force_thresholds_nominal[i] = higher_force[i]

        res = self.panda_set_collision_behaviour(SetForceTorqueCollisionBehavior_req)

        return res

#-------
    def close_gripper(self, grasping_width, grasping_vel, grasping_force, grasping_homing, grasping_close, grasping_move):

        req = PandaGripperSrvRequest()

        req.gripper_move = grasping_move
        req.gripper_homing = grasping_homing
        req.gripper_close = grasping_close
        req.grasping_width = grasping_width
        req.grasping_speed = grasping_vel
        req.grasping_force = grasping_force

        res = self.robot_gripper_srv(req)

        return res.success

#-------
    def VelocityProfile(self, t, vInit, vFinal, alphaInit, alphaFinal, t0, tConv):

        if t<t0:

            v = vInit * 2.0/math.pi*np.arctan(alphaInit*t)

        else:

            a1 = (vFinal - vInit*2.0/math.pi*np.arctan(alphaInit*t0))/(1.0 - 2.0/math.pi*np.arctan(alphaFinal*(t0 - tConv)))
            a2 = vFinal - a1

            v = a1 * 2.0/math.pi*np.arctan(alphaFinal*(t - tConv)) + a2

        return v

#-------
    def InitURDF(self, time_step, urdf_filename, robot_base, robot_orientation):

        #----- This function is kept in case the user wants to use the PyBullet
        # library for calculating the jacobian instead of the libfranka library -----

        id_simulator = p.connect(p.DIRECT)
        p.setTimeStep(time_step)

        p.setAdditionalSearchPath(pybullet_data.getDataPath())

        p.loadURDF("plane.urdf")
        p.setGravity(0, 0, -9.81, id_simulator)

        id_robot = p.loadURDF(urdf_filename, robot_base, robot_orientation, useFixedBase=True, physicsClientId=id_simulator)

        #----- Take info from URDF -----

        joint_idx_arm = [1, 2, 3, 4, 5, 6, 7]
        joint_idx_fingers = [0, 0]
        joint_idx_hand = 0
        arm_base_link_idx = -100
        arm_ee_link_idx = -100
        link_name_to_index = None

        link_name_to_index = {p.getBodyInfo(id_robot)[0]: -1}
        num_joints = p.getNumJoints(id_robot)

        for i in range(num_joints):

            info = p.getJointInfo(id_robot, i)
            joint_name = info[1] if type(info[1]) is str else info[1].decode("utf-8")

            if "panda_joint" in joint_name and len(joint_name) == 12:

                joint_num = int(joint_name.split("panda_joint")[1])
                if joint_num < 8:

                    joint_idx_arm[joint_num - 1] = i

                if joint_num == 1:

                        arm_base_link_idx = info[16]

            elif "panda_hand_joint" in joint_name:

                arm_ee_link_idx = info[16]
                joint_idx_hand = i

            elif "panda_finger_joint" in joint_name:

                joint_num = int(joint_name.split("panda_finger_joint")[1])
                joint_idx_fingers[joint_num - 1] = i

            _name = info[12] if type(info[12]) is str else info[12].decode("utf-8")
            link_name_to_index[_name] = i

        return id_simulator, id_robot, joint_idx_arm, joint_idx_fingers, joint_idx_hand, arm_base_link_idx, arm_ee_link_idx, link_name_to_index

#-------
    def CalculateVars_usingSimulator(self, arm_state_msg, base_state_msg, force_msg, ee_state_msg, joint_idx_arm, model, arm_ee_link_idx, arm_base_link_idx, link_name_to_index):

        #----- INFO FROM JOINT STATE -----

        id_start =3

        q = []
        q_dot = []
        tau = []

        for i in range(id_start, id_start+7):

            q.append(arm_state_msg.position[i])
            q_dot.append(arm_state_msg.velocity[i])
            tau.append(arm_state_msg.effort[i])

        self.q = np.array(q)
        self.q_dot = np.array(q_dot)
        self.tau = np.array(tau)

        #----- Set joints in PyBuller simulation -----

        for i in range(len(joint_idx_arm)):
            p.resetJointState(model, joint_idx_arm[i], q[i], q_dot[i])            # Set the robot joints in simulator to appropriate values

        #----- GET INFO FROM SIMULATION -----

        zero_vec =[0.0] * 9
        lin, ang = p.calculateJacobian(model, arm_ee_link_idx, [0.0, 0.0, 0.0], list(q) + [0.0, 0.0], zero_vec, zero_vec)
        lin = np.array(lin)
        ang = np.array(ang)

        self.J_b_ee = np.concatenate((lin, ang), axis=0)

        self.M = np.array(p.calculateMassMatrix(model, list(q)+ [0.0, 0.0]))
        self.b = np.array(p.calculateInverseDynamics(model, list(q)+ [0.0, 0.0], list(q_dot)+ [0.0, 0.0], zero_vec))

        link_pos_and_vel = p.getLinkStates(model, linkIndices=[arm_base_link_idx, link_name_to_index["panda_default_EE"]],
            computeLinkVelocity = 1, computeForwardKinematics = True
        )

        #----- Values in the simulator -----

        C_O_b = R.from_quat(link_pos_and_vel[0][5])
        r_O_b = link_pos_and_vel[0][4]
        C_O_ee = R.from_quat(link_pos_and_vel[1][5])
        r_O_ee = link_pos_and_vel[1][4]

        C_b_ee = C_O_b.inv() * C_O_ee
        C_b_ee_mat = C_b_ee.as_dcm()

        r_delta = list(np.array(r_O_ee) - np.array(r_O_b))
        r_b_ee = C_O_b.inv().apply(r_delta)

        self.T_b_ee = np.array([[C_b_ee_mat[0, 0], C_b_ee_mat[0, 1], C_b_ee_mat[0, 2], r_b_ee[0]],
                                [C_b_ee_mat[1, 0], C_b_ee_mat[1, 1], C_b_ee_mat[1, 2], r_b_ee[1]],
                                [C_b_ee_mat[2, 0], C_b_ee_mat[2, 1], C_b_ee_mat[2, 2], r_b_ee[2]],
                                [0.0             , 0.0             , 0.0             , 1.0      ]])

        #----- INFO FROM FORCE MSG -----

        self.force = np.array([force_msg.wrench.force.x, force_msg.wrench.force.y, force_msg.wrench.force.z])

        #----- INFO FROM BASE ODOM -----

        self.linVelBase = [base_state_msg.twist.twist.linear.x, base_state_msg.twist.twist.linear.y, 0.0]
        self.angVelBase = [0.0, 0.0, base_state_msg.twist.twist.angular.z]

        C_O_b = R.from_quat([base_state_msg.pose.pose.orientation.x, base_state_msg.pose.pose.orientation.y, base_state_msg.pose.pose.orientation.z, base_state_msg.pose.pose.orientation.w])
        C_O_b_mat = C_O_b.as_dcm()

        self.T_O_b = np.array([[C_O_b_mat[0, 0], C_O_b_mat[0, 1], C_O_b_mat[0, 2], base_state_msg.pose.pose.position.x],
                               [C_O_b_mat[1, 0], C_O_b_mat[1, 1], C_O_b_mat[1, 2], base_state_msg.pose.pose.position.y],
                               [C_O_b_mat[2, 0], C_O_b_mat[2, 1], C_O_b_mat[2, 2], base_state_msg.pose.pose.position.z],
                               [0.0            , 0.0            , 0.0            , 1.0                                ]])

#-------
    def CalculateVars_usingPanda(self, panda_model, base_state_msg):

        #----- INFO FROM JOINT STATE -----

        q = []
        q_dot = []
        tau = []

        for i in range(7):

            q.append(panda_model.q_d[i])
            q_dot.append(panda_model.dq_d[i])
            tau.append(panda_model.tau[i])

        self.q = np.array(q)
        self.q_dot = np.array(q_dot)
        self.tau = np.array(tau)

        self.T_b_ee = np.array(panda_model.O_T_EE)
        self.T_b_ee = np.transpose(self.T_b_ee.reshape(4, 4))

        self.T_ee_k = np.array(panda_model.EE_T_K)
        self.T_ee_k = np.transpose(self.T_ee_k.reshape(4, 4))

        self.T_b_ee = np.matmul(self.T_b_ee, self.T_ee_k)                        # Stiffness frame is actualy the EE frame we used in the simulation

        #----- GET INFO FROM MODEL STATE -----

        self.J_b_ee = np.array(panda_model.jacobian)                             # It is saved as column major but python does everyhing row major
        self.J_b_ee = np.transpose(self.J_b_ee.reshape(7, 6))

        self.M = np.array(panda_model.mass_matrix)
        self.M = np.transpose(self.M.reshape(7,7))

        self.b = np.array(panda_model.coriolis)
        self.g = np.array(panda_model.gravity)

        self.b += self.g

        #----- GET FORCE INFO -----

        ext_wrench = np.array(panda_model.K_F_ext_hat_K)
        self.force = -ext_wrench[:3]

        #----- INFO FROM BASE ODOM -----

        self.linVelBase = [base_state_msg.twist.twist.linear.x, base_state_msg.twist.twist.linear.y, 0.0]
        self.angVelBase = [0.0, 0.0, base_state_msg.twist.twist.angular.z]

        C_O_b = R.from_quat([base_state_msg.pose.pose.orientation.x, base_state_msg.pose.pose.orientation.y, base_state_msg.pose.pose.orientation.z, base_state_msg.pose.pose.orientation.w])
        C_O_b_mat = C_O_b.as_dcm()

        self.T_O_b = np.array([[C_O_b_mat[0, 0], C_O_b_mat[0, 1], C_O_b_mat[0, 2], base_state_msg.pose.pose.position.x],
                               [C_O_b_mat[1, 0], C_O_b_mat[1, 1], C_O_b_mat[1, 2], base_state_msg.pose.pose.position.y],
                               [C_O_b_mat[2, 0], C_O_b_mat[2, 1], C_O_b_mat[2, 2], base_state_msg.pose.pose.position.z],
                               [0.0            , 0.0            , 0.0            , 1.0                                ]])

#-------
    def run_once(self, t, vInit, vFinal, alphaInit, alphaFinal, t0, tConv, alpha=0.1, smooth=False, mixCoeff=0.1):

        #----- Copying -----

        try:
            base_state_msg = Odometry()                                              # Base position and velocity

            self.processing = True

            base_state_msg = self.baseState_msg

            req = PandaStateSrvRequest()
            panda_model = self.panda_model_state_srv(req)

            self.processing = False

            #----- Update values using URDF model -----

            self.CalculateVars_usingPanda(panda_model, base_state_msg)

            #----- Calculate Info -----

            T_O_ee = np.matmul(self.T_O_b, self.T_b_ee)

            C_O_ee = R.from_dcm(T_O_ee[:3, :3])
            C_O_b = R.from_dcm(self.T_O_b[:3, :3])
            C_b_ee = R.from_dcm(self.T_b_ee[:3, :3])

            r_O_ee = np.squeeze(np.copy(T_O_ee[:3, 3]))

            self.world_ee_pos.append(r_O_ee)

            #----- Update Buffers and estimatein direction_estimator class -----

            if len(self.direction_estimator.measuredForcesBuffer)>0:

                self.direction_estimator.UpdateBuffers(self.force - np.squeeze(self.direction_estimator.measuredForcesBuffer[0]), r_O_ee)
                self.direction_estimator.UpdateEstimate(self.force, alpha, C_O_ee, smooth, mixCoeff)

            else:

                self.direction_estimator.UpdateBuffers(self.force, r_O_ee)

            #----- Get linear velocity magnitude from the velocity profile -----

            velProfile = self.VelocityProfile(t, vInit, vFinal, alphaInit, alphaFinal, t0, tConv)

            veldesEE_ee = self.direction_estimator.GetPlannedVelocities(v=velProfile, calcAng=True, kAng=0.3)   #Change to 0.05 or false

            r_b_ee = self.T_b_ee[:3, 3]

            infoTuple = (self.M, self.b, self.J_b_ee, self.q, self.q_dot, C_O_b, C_O_ee, C_b_ee, r_b_ee, velProfile, self.tau)

            try:
                temp = self.controller.PerformOneStep(veldesEE_ee, infoTuple)

                self.publishArmAndBaseVelocityControl(self.controller.GetCurrOptSol(), linVelBase = self.controller.vLinBase_b, angVelBase = self.controller.vAngBase_b[2])

            except:

                self.publishArmAndBaseVelocityControl([0.0]*7, linVelBase = [0.0, 0.0, 0.0], angVelBase = 0.0)

        except rospy.ServiceException as e:

            print("Service failed: " + str(e))

#-------
    def prepare_for_stop(self):

        self.publishArmAndBaseVelocityControl([0.0]*7, linVelBase = [0.0, 0.0, 0.0], angVelBase = 0.0)

#-------
    def AlignZAxis(self):

        #----- Function used to test angular velocity planning -----

        # It should align the z axis of the EE frame with the horizontal plane

        N_align = 1000
        theta_des = 0
        kAng=0.2

        for i in range(N_align):

            print("Iteration: "+str(i))

            req = PandaStateSrvRequest()
            panda_model = self.panda_model_state_srv(req)
            base_state_msg = self.baseState_msg

            self.CalculateVars_usingPanda(panda_model, base_state_msg)

            C_b_ee = R.from_dcm(self.T_b_ee[:3, :3])

            g_ee = C_b_ee.inv().apply([0.0, 0.0, -1.0])

            theta = np.arccos(np.dot(np.array(g_ee), np.array([0.0, 0.0, 1.0])))

            if theta>np.pi/4 and theta<3*np.pi/4:

                orthoProjMat = OrthoProjection(g_ee)
                z_proj = np.matmul(orthoProjMat, np.array([0.0, 0.0, 1.0]))
                n_ee = np.cross(np.array([0.0, 0.0, 1.0]), z_proj)
                wdesEE_ee = kAng*(np.pi/2-theta - theta_des)*n_ee

                vdesEE_ee = np.array(3*[0.0])
                veldesEE_ee = np.concatenate((np.squeeze(vdesEE_ee), np.squeeze(wdesEE_ee)), axis=0)

                T_O_ee = np.matmul(self.T_O_b, self.T_b_ee)

                C_O_ee = R.from_dcm(T_O_ee[:3, :3])
                C_O_b = R.from_dcm(self.T_O_b[:3, :3])
                C_b_ee = R.from_dcm(self.T_b_ee[:3, :3])

                r_O_ee = np.squeeze(np.copy(T_O_ee[:3, 3]))
                r_b_ee = self.T_b_ee[:3, 3]

                infoTuple = (self.M, self.b, self.J_b_ee, self.q, self.q_dot, C_O_b, C_O_ee, C_b_ee, r_b_ee, 0.0, self.tau)

                temp = self.cinit.PerformOneStep(veldesEE_ee, infoTuple)
                self.publishArmAndBaseVelocityControl(self.cinit.GetCurrOptSol(), linVelBase = [0.0, 0.0, 0.0], angVelBase = 0.0)

#-------
    def InitProgram(self):

        #----- Function that performs the initial direction estimation movement -----

        print(5*"-"+' Init procedure '+5*'-')

        direction_list = self.direction_estimator.CalculateInitialDirections()

        try_velocity = 0.005
        N_steps_per_dir = 3

        totalInitTime = 0.0
        X_data = []
        Y_data = []

        startTime = time.time()

        for idx in range(len(direction_list)):

            d = direction_list[idx]

            print("Trying the direction n: "+str(d))

            if idx==0:

                req = PandaStateSrvRequest()
                panda_model = self.panda_model_state_srv(req)

                sf = np.array(panda_model.K_F_ext_hat_K[:3])

            for temp_it in range(N_steps_per_dir):

                req = PandaStateSrvRequest()
                panda_model = self.panda_model_state_srv(req)
                base_state_msg = self.baseState_msg

                self.CalculateVars_usingPanda(panda_model, base_state_msg)

                T_O_ee = np.matmul(self.T_O_b, self.T_b_ee)

                C_O_ee = R.from_dcm(T_O_ee[:3, :3])
                C_O_b = R.from_dcm(self.T_O_b[:3, :3])
                C_b_ee = R.from_dcm(self.T_b_ee[:3, :3])

                r_O_ee = np.squeeze(np.copy(T_O_ee[:3, 3]))
                r_b_ee = self.T_b_ee[:3, 3]

                infoTuple = (self.M, self.b, self.J_b_ee, self.q, self.q_dot, C_O_b, C_O_ee, C_b_ee, r_b_ee, try_velocity, self.tau)

                veldesEE_ee = try_velocity*np.array(d + 3*[0.0])

                try:
                    temp = self.cinit.PerformOneStep(veldesEE_ee, infoTuple)

                    self.publishArmAndBaseVelocityControl(self.cinit.GetCurrOptSol(), linVelBase = [0.0, 0.0, 0.0], angVelBase = 0.0)

                except:

                    self.publishArmAndBaseVelocityControl([0.0]*7, linVelBase = [0.0, 0.0, 0.0], angVelBase = 0.0)

            req = PandaStateSrvRequest()
            panda_model = self.panda_model_state_srv(req)

            f = np.array(panda_model.K_F_ext_hat_K[:3])

            y = 1 - LA.norm(f)/LA.norm(sf)

            if y>0:

                X_data.append(d[:2])
                Y_data.append(y)

            for temp_it in range(N_steps_per_dir):

                req = PandaStateSrvRequest()
                panda_model = self.panda_model_state_srv(req)
                base_state_msg = self.baseState_msg

                self.CalculateVars_usingPanda(panda_model, base_state_msg)

                T_O_ee = np.matmul(self.T_O_b, self.T_b_ee)

                C_O_ee = R.from_dcm(T_O_ee[:3, :3])
                C_O_b = R.from_dcm(self.T_O_b[:3, :3])
                C_b_ee = R.from_dcm(self.T_b_ee[:3, :3])

                r_O_ee = np.squeeze(np.copy(T_O_ee[:3, 3]))
                r_b_ee = self.T_b_ee[:3, 3]

                infoTuple = (self.M, self.b, self.J_b_ee, self.q, self.q_dot, C_O_b, C_O_ee, C_b_ee, r_b_ee, try_velocity, self.tau)

                veldesEE_ee = -try_velocity*np.array(d + 3*[0.0])

                try:
                    temp = self.cinit.PerformOneStep(veldesEE_ee, infoTuple)

                    self.publishArmAndBaseVelocityControl(self.cinit.GetCurrOptSol(), linVelBase = [0.0, 0.0, 0.0], angVelBase = 0.0)

                except:

                    self.publishArmAndBaseVelocityControl([0.0]*7, linVelBase = [0.0, 0.0, 0.0], angVelBase = 0.0)

            req = PandaStateSrvRequest()
            panda_model = self.panda_model_state_srv(req)

            sf = np.array(panda_model.K_F_ext_hat_K[:3])

        self.direction_estimator.EstimateBestInitialDirection(X_data, Y_data, C_O_ee, C_b_ee)

        stopTime = time.time()
        totalInitTime = stopTime - startTime

        print("Total init time: "+str(totalInitTime))

#-------
    def RecordTrueInitDirection(self):

        #----- Function that records the z axis of the EE frame expressed in the body frame -----

        req = PandaStateSrvRequest()
        panda_model = self.panda_model_state_srv(req)
        base_state_msg = self.baseState_msg

        self.CalculateVars_usingPanda(panda_model, base_state_msg)

        T_O_ee = np.matmul(self.T_O_b, self.T_b_ee)

        C_O_ee = R.from_dcm(T_O_ee[:3, :3])
        C_O_b = R.from_dcm(self.T_O_b[:3, :3])

        r_O_ee = np.squeeze(np.copy(T_O_ee[:3, 3]))

        gravity_dir = np.array([0.0, 0.0, 1.0]).reshape(-1, 1)
        orthoProjMatGravity = OrthoProjection(gravity_dir)

        true_init_dir = np.squeeze(np.matmul(T_O_ee[:3, :3], np.array([0.0, 0.0, -1.0]).reshape(-1,1)))
        self.true_init_dir = np.matmul(orthoProjMatGravity, true_init_dir)

        print("True Init dir: "+str(self.true_init_dir))




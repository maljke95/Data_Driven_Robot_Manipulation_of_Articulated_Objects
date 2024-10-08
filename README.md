# Data Driven Robot Manipulation of Articulated Objects

Master thesis project ETH Zurich, Autonomous Systems Lab (ASL)

**Description:** Designed a whole-body controller for a mobile robot with an arm capable of opening various types of doors.
The door model was estimated in real-time using data solely from joint encoders and force/torque sensors.
Sequential Hierarchical Quadratic Optimization was applied to plan joint torques, while SOCP was used for
arm and base trajectory planning. The algorithms were initially tested in Pybullet and Gazebo simulations,
followed by successful implementation on a real Franka Emika robot.


Base code for this project was taken from the ASL's main repository of the mobile manipulation (moma) team at ASL containing launch files, robot descriptions, utilities, controllers, and documentation for robot operation: https://github.com/ethz-asl/moma/

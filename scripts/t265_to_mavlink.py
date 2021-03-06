




#!/usr/bin/env python3

#####################################################
##          librealsense T265 to MAVLink           ##
#####################################################
# This script assumes pyrealsense2.[].so file is found under the same directory as this script
# Install required packages:
#   pip install pyrealsense2
#   pip3 install transformations
#   pip3 install dronekit
#   pip3 install apscheduler

 # Set the path for IDLE
import sys
sys.path.append("/usr/local/lib/")
sys.path.append("/usr/local/lib/python3.5/dist-packages")

# Set MAVLink protocol to 2.
import os
os.environ["MAVLINK20"] = "1"

# Import the libraries
import io
import base64
import pyrealsense2 as rs
import numpy as np
import transformations as tf
import math as m
import time
import argparse
import threading
import paho.mqtt.client as mqtt

from time import sleep
from apscheduler.schedulers.background import BackgroundScheduler

from dronekit import connect, VehicleMode, Vehicle
from pymavlink import mavutil
from PIL import Image

MAV_STATE_UNINIT        = 0
MAV_STATE_BOOT          = 1
MAV_STATE_CALIBRATING   = 2
MAV_STATE_STANDBY       = 3
MAV_STATE_ACTIVE        = 4
MAV_STATE_CRITICAL      = 5
MAV_STATE_EMERGENCY     = 6
MAV_STATE_HILSIM        = 7
MAV_STATE_POWEROFF      = 8

#######################################
# Parameters
#######################################

# Default configurations for connection to the FCU
connection_string_default = '/dev/ttyUSB1'
connection_baudrate_default = 921600
connection_timeout_sec_default = 5
vision_msg_hz_default = 30
confidence_msg_hz_default = 1
camera_orientation_default = 0

# In NED frame, offset from the IMU or the center of gravity to the camera's origin point
body_offset_enabled = 0
body_offset_x = 0.05    # In meters (m), so 0.05 = 5cm
body_offset_y = 0       # In meters (m)
body_offset_z = 0       # In meters (m)

# Global scale factor, position x y z will be scaled up/down by this factor
scale_factor = 1.0

# Enable using yaw from compass to align north (zero degree is facing north)
compass_enabled = 0

# Default global position of home/ origin
home_lat = 0       # Somewhere in Africa
home_lon = 0        # Somewhere in Africa
home_alt = 0

vehicle = None
_isflying = False
is_vehicle_connected = False
pipe = None

# pose data confidence: 0x0 - Failed / 0x1 - Low / 0x2 - Medium / 0x3 - High
pose_data_confidence_level = ('Failed', 'Low', 'Medium', 'High')

#######################################
# Parsing user' inputs
#######################################

parser = argparse.ArgumentParser(description='Reboots vehicle')
parser.add_argument('--connect',
                    help="Vehicle connection target string. If not specified, a default string will be used.")
parser.add_argument('--baudrate', type=float,
                    help="Vehicle connection baudrate. If not specified, a default value will be used.")
parser.add_argument('--vision_msg_hz', type=float,
                    help="Update frequency for VISION_POSITION_ESTIMATE message. If not specified, a default value will be used.")
parser.add_argument('--confidence_msg_hz', type=float,
                    help="Update frequency for confidence level. If not specified, a default value will be used.")
parser.add_argument('--scale_calib_enable', default=False, action='store_true',
                    help="Scale calibration. Only run while NOT in flight")
parser.add_argument('--camera_orientation', type=int,
                    help="Configuration for camera orientation. Currently supported: forward, usb port to the right - 0; downward, usb port to the right - 1")
parser.add_argument('--auto_set_ekf_home_enable', default=True, action='store_false',
                    help="Enable auto setting EKF home")
parser.add_argument('--debug_enable',type=int,
                    help="Enable debug messages on terminal")


args = parser.parse_args()

connection_string = args.connect
connection_baudrate = args.baudrate
vision_msg_hz = args.vision_msg_hz
confidence_msg_hz = args.confidence_msg_hz
scale_calib_enable = args.scale_calib_enable
camera_orientation = args.camera_orientation
auto_set_ekf_home_enable = args.auto_set_ekf_home_enable
debug_enable = args.debug_enable

# Using default values if no specified inputs
if not connection_string:
    connection_string = connection_string_default
    print("INFO: Using default connection_string", connection_string, flush=True)
else:
    print("INFO: Using connection_string", connection_string, flush=True)

if not connection_baudrate:
    connection_baudrate = connection_baudrate_default
    print("INFO: Using default connection_baudrate", connection_baudrate, flush=True)
else:
    print("INFO: Using connection_baudrate", connection_baudrate, flush=True)

if not vision_msg_hz:
    vision_msg_hz = vision_msg_hz_default
    print("INFO: Using default vision_msg_hz", vision_msg_hz, flush=True)
else:
    print("INFO: Using vision_msg_hz", vision_msg_hz, flush=True)

if not confidence_msg_hz:
    confidence_msg_hz = confidence_msg_hz_default
    print("INFO: Using default confidence_msg_hz", confidence_msg_hz, flush=True)
else:
    print("INFO: Using confidence_msg_hz", confidence_msg_hz, flush=True)

if body_offset_enabled == 1:
    print("INFO: Using camera position offset: Enabled, x y z is", body_offset_x, body_offset_y, body_offset_z, flush=True)
else:
    print("INFO: Using camera position offset: Disabled", flush=True)

if compass_enabled == 1:
    print("INFO: Using compass: Enabled. Heading will be aligned to north.", flush=True)
else:
    print("INFO: Using compass: Disabled", flush=True)

if scale_calib_enable == True:
    print("\nINFO: SCALE CALIBRATION PROCESS. DO NOT RUN DURING FLIGHT.\nINFO: TYPE IN NEW SCALE IN FLOATING POINT FORMAT\n", flush=True)
else:
    if scale_factor == 1.0:
        print("INFO: Using default scale factor", scale_factor, flush=True)
    else:
        print("INFO: Using scale factor", scale_factor, flush=True)

if not camera_orientation:
    camera_orientation = camera_orientation_default
    print("INFO: Using default camera orientation", camera_orientation, flush=True)
else:
    print("INFO: Using camera orientation", camera_orientation, flush=True)

# Transformation to convert different camera orientations to NED convention. Replace camera_orientation_default for your configuration.
#   0: Forward, USB port to the right
#   1: Downfacing, USB port to the right
#   2: Forward, 45 degree tilted down
# Important note for downfacing camera: you need to tilt the vehicle's nose up a little - not flat - before you run the script, otherwise the initial yaw will be randomized, read here for more details: https://github.com/IntelRealSense/librealsense/issues/4080. Tilt the vehicle to any other sides and the yaw might not be as stable.

if camera_orientation == 0:
    # Forward, USB port to the right
    H_aeroRef_T265Ref = np.array([[0,0,-1,0],[1,0,0,0],[0,-1,0,0],[0,0,0,1]])
    H_T265body_aeroBody = np.linalg.inv(H_aeroRef_T265Ref)
elif camera_orientation == 1:
    # Downfacing, USB port to the right
    H_aeroRef_T265Ref = np.array([[0,0,-1,0],[1,0,0,0],[0,-1,0,0],[0,0,0,1]])
    H_T265body_aeroBody = np.array([[0,1,0,0],[1,0,0,0],[0,0,-1,0],[0,0,0,1]])
elif camera_orientation == 2:
    # 45degree forward
    H_aeroRef_T265Ref = np.array([[0,0,-1,0],[1,0,0,0],[0,-1,0,0],[0,0,0,1]])
    H_T265body_aeroBody = np.array(
        [[ 0.       ,  1.        ,  0.        ,  0.        ],
        [-0.70710676, -0.        , -0.70710676, -0.        ],
        [-0.70710676,  0.        ,  0.70710676,  0.        ],
        [ 0.        ,  0.        ,  0.        ,  1.        ]])
else:
    # Default is facing forward, USB port to the right
    H_aeroRef_T265Ref = np.array([[0,0,-1,0],[1,0,0,0],[0,-1,0,0],[0,0,0,1]])
    H_T265body_aeroBody = np.linalg.inv(H_aeroRef_T265Ref)

if auto_set_ekf_home_enable == False:
    print("INFO: Automatically set EKF home: DISABLED", flush=True)
else:
    print("INFO: Automatically set EKF home: Enabled", flush=True)

if not debug_enable:
    debug_enable = 0
else:
    debug_enable = 1
    np.set_printoptions(precision=4, suppress=True) # Format output on terminal
    print("INFO: Debug messages enabled.", flush=True)

#######################################
# Functions
#######################################

# https://mavlink.io/en/messages/common.html#VISION_POSITION_ESTIMATE
def send_vision_position_message():
    global is_vehicle_connected, current_time, H_aeroRef_aeroBody

    if is_vehicle_connected == True and H_aeroRef_aeroBody is not None:
        rpy_rad = np.array( tf.euler_from_matrix(H_aeroRef_aeroBody, 'sxyz'))

        msg = vehicle.message_factory.vision_position_estimate_encode(
            current_time,                       # us Timestamp (UNIX time or time since system boot)
            H_aeroRef_aeroBody[0][3],	        # Global X position
            H_aeroRef_aeroBody[1][3],           # Global Y position
            H_aeroRef_aeroBody[2][3],	        # Global Z position
            rpy_rad[0],	                        # Roll angle
            rpy_rad[1],	                        # Pitch angle
            rpy_rad[2]	                        # Yaw angle
        )

        vehicle.send_mavlink(msg)
        vehicle.flush()

# For a lack of a dedicated message, we pack the confidence level into a message that will not be used, so we can view it on GCS
# Confidence level value: 0 - 3, remapped to 0 - 100: 0% - Failed / 33.3% - Low / 66.6% - Medium / 100% - High
def send_confidence_level_dummy_message():
    global is_vehicle_connected, data, current_confidence
    if is_vehicle_connected == True and data is not None:
        # Show confidence level on terminal
        print("INFO: Tracking confidence: ", pose_data_confidence_level[data.tracker_confidence], flush=True)

        # Send MAVLink message to show confidence level numerically
        msg = vehicle.message_factory.vision_position_delta_encode(
            0,	            #us	Timestamp (UNIX time or time since system boot)
            0,	            #Time since last reported camera frame
            [0, 0, 0],      #angle_delta
            [0, 0, 0],      #position_delta
            float(data.tracker_confidence * 100 / 3)
        )
        vehicle.send_mavlink(msg)
        vehicle.flush()

        # If confidence level changes, send MAVLink message to show confidence level textually and phonetically
        if current_confidence is None or current_confidence != data.tracker_confidence:
            current_confidence = data.tracker_confidence
            confidence_status_string = 'Tracking confidence: ' + pose_data_confidence_level[data.tracker_confidence]
            status_msg = vehicle.message_factory.statustext_encode(
                3,	            #severity, defined here: https://mavlink.io/en/messages/common.html#MAV_SEVERITY, 3 will let the message be displayed on Mission Planner HUD
                confidence_status_string.encode()	  #text	char[50]
            )
            vehicle.send_mavlink(status_msg)
            vehicle.flush()


# Send a mavlink SET_GPS_GLOBAL_ORIGIN message (http://mavlink.org/messages/common#SET_GPS_GLOBAL_ORIGIN), which allows us to use local position information without a GPS.
def set_default_global_origin():
    if  is_vehicle_connected == True:
        msg = vehicle.message_factory.set_gps_global_origin_encode(
            int(vehicle._master.source_system),
            home_lat,
            home_lon,
            home_alt
        )

        vehicle.send_mavlink(msg)
        vehicle.flush()

# Send a mavlink SET_HOME_POSITION message (http://mavlink.org/messages/common#SET_HOME_POSITION), which allows us to use local position information without a GPS.
def set_default_home_position():
    if  is_vehicle_connected == True:
        x = 0
        y = 0
        z = 0
        q = [1, 0, 0, 0]   # w x y z

        approach_x = 0
        approach_y = 0
        approach_z = 1

        msg = vehicle.message_factory.set_home_position_encode(
            int(vehicle._master.source_system),
            home_lat,
            home_lon,
            home_alt,
            x,
            y,
            z,
            q,
            approach_x,
            approach_y,
            approach_z
        )

        vehicle.send_mavlink(msg)
        vehicle.flush()


def is_flying():
    global _isflying
    if((vehicle._vehicle_type != mavutil.mavlink.MAV_TYPE_QUADROTOR) and
        (vehicle._vehicle_type != mavutil.mavlink.MAV_TYPE_FIXED_WING)):
        return None

    isFlying = ((vehicle._system_status == MAV_STATE_ACTIVE) or
        ((_isflying == True) and (vehicle._system_status == MAV_STATE_CRITICAL or vehicle._system_status == MAV_STATE_EMERGENCY)))

    _isflying = isFlying
    return _isflying

def arm():
    vehicle.armed   = True

def disarm():
    vehicle.armed   = False

def takeoff():
    vehicle.simple_takeoff(1.2)

def stabilize():
    vehicle.mode = VehicleMode("STABILIZE")

def loiter():
    vehicle.mode = VehicleMode("LOITER")

def altHold():
    vehicle.mode = VehicleMode("ALTITUDE_HOLD")

def land():
    vehicle.mode = VehicleMode("LAND")

def mission_start():
    vehicle.mode = VehicleMode("AUTO")

# Request a timesync update from the flight controller, for future work.
# TODO: Inspect the usage of timesync_update
def update_timesync(ts=0, tc=0):
    if ts == 0:
        ts = int(round(time.time() * 1000))
    msg = vehicle.message_factory.timesync_encode(
        tc,     # tc1
        ts      # ts1
    )
    vehicle.send_mavlink(msg)
    vehicle.flush()

# Listen to messages that indicate EKF is ready to set home, then set EKF home automatically.
def statustext_callback(self, attr_name, value):
    # These are the status texts that indicates EKF is ready to receive home position
    if is_vehicle_connected == True and value.text == "GPS Glitch" or value.text == "GPS Glitch cleared" or value.text == "EKF2 IMU0 ext nav yaw alignment complete":
        time.sleep(0.1)
        print("INFO: Set EKF home with default GPS location", flush=True)
        set_default_global_origin()
        set_default_home_position()

# Listen to attitude data to acquire heading when compass data is enabled
def att_msg_callback(self, attr_name, value):
    global heading_north_yaw
    if heading_north_yaw is None:
        heading_north_yaw = value.yaw
        print("INFO: Received first ATTITUDE message with heading yaw", heading_north_yaw * 180 / m.pi, "degrees", flush=True)
    else:
        heading_north_yaw = value.yaw
        print("INFO: Received ATTITUDE message with heading yaw", heading_north_yaw * 180 / m.pi, "degrees", flush=True)

def vehicle_connect():
    global vehicle, is_vehicle_connected

    try:
        vehicle = connect(connection_string, wait_ready = True, baud = connection_baudrate, source_system = 1)
    except:
        print('Connection error! Retrying...', flush=True)
        sleep(1)

    if vehicle == None:
        is_vehicle_connected = False
        return False
    else:
        is_vehicle_connected = True
        return True

def realsense_connect():
    global pipe
    # Declare RealSense pipeline, encapsulating the actual device and sensors
    pipe = rs.pipeline()

    # Build config object before requesting data
    cfg = rs.config()

    # Enable the stream we are interested in
    cfg.enable_stream(rs.stream.pose) # Positional data
    cfg.enable_stream(rs.stream.fisheye, 1) # Left camera
    cfg.enable_stream(rs.stream.fisheye, 2) # Right camera
    # cfg.enable_all_streams()

    # Start streaming with requested config
    pipe.start(cfg)


# Monitor user input from the terminal and update scale factor accordingly
def scale_update():
    global scale_factor
    while True:
        scale_factor = float(input("INFO: Type in new scale as float number\n"))
        print("INFO: New scale is ", scale_factor, flush=True)

def send_image(frame, mqtt_client, topic):
    try:
        data = frame.get_data();
        nparr = np.asanyarray(data)
        img = Image.fromarray(nparr)
        buf = io.BytesIO()
        img.save(buf, format='JPEG')
        bytes = buf.getvalue()
        content = base64.b64encode(bytes)
        mqtt_client.publish(topic, content)
    except Exception as err:
        print("Error sending image: " + str(err), flush=True)

#######################################
# MQTT Setup
#######################################

# The callback for when the client receives a CONNACK response from the server.
def on_connect(client, userdata, flags, rc):
    print("INFO: Connected to MQTT server with result code "+str(rc), flush=True)
    client.publish("presence", "drone")
    client.subscribe("command")

# The callback for when a PUBLISH message is received from the server.
def on_message(client, userdata, msg):
    print("INFO: message received, topic: " + msg.topic + ", payload: " + msg.payload.decode(), flush=True)
    if msg.topic == "command":
        cmd = msg.payload.decode("utf-8")
        print("INFO: command received: " + cmd, flush=True)
        print("is flying: " + str(is_flying()))
        handle_cmd(cmd)

def handle_cmd(cmd):
    try:
        if cmd == "arm":
            if is_flying():
                print("INFO: Vehicle is flying, don't arm again!")
            else:
                print("INFO: arming...", flush=True)
                arm()
        elif cmd == "disarm":
            print("INFO: disarming...", flush=True)
            disarm()
        elif cmd == "takeoff":
            if is_flying():
                print("INFO: Vehicle is flying, don't takeof again!")
            else:
                print("INFO: takeoff!", flush=True)
                takeoff()
        elif cmd == "land":
            print("INFO: Land!", flush=True)
            land()
        elif cmd == "stabilize":
            if is_flying():
                print("INFO: Vehicle is flying, won't change mode to stabilize")
            else:
                print("INFO: Mode Stabilize", flush=True)
                stabilize()
        elif cmd == "loiter":
            if is_flying():
                print("INFO: Vehicle is flying, won't change mode to loiter")
            else:
                print("INFO: Mode Loiter", flush=True)
                loiter()
        elif cmd == "altHold":
            if is_flying():
                print("INFO: Vehicle is flying, won't change mode to altitude hold")
            else:
                print("INFO: Mode Altitude Hold!", flush=True)
                altHold()
        elif cmd == "mission_start":
            if is_flying():
                print("INFO: Vehicle is flying, won't start mission")
            else:
                print("INFO: mission_start!", flush=True)
                mission_start()
        else:
            print("INFO: received unrecognized command: " + cmd, flush=True)
    except Exeption as err:
        print("Error: " + err, flush=True)

client = mqtt.Client()
client.on_connect = on_connect
client.on_message = on_message

#######################################
# Main code starts here
#######################################

print("INFO: Connecting to MQTT server.", flush=True)
client.connect_async("192.168.2.64", 1883, 10)
# client.connect("192.168.2.64", 1883, 10)
client.loop_start()
client.publish("status", "drone online")

print("INFO: Connecting to Realsense camera.", flush=True)
client.publish("status", "connecting to realsense")
realsense_connect()
print("INFO: Realsense connected.", flush=True)

client.publish("status", "connecting to vehicle")
print("INFO: Connecting to vehicle.", flush=True)
while (not vehicle_connect()):
    pass
print("INFO: Vehicle connected.", flush=True)
client.publish("status", "vehicle connected")

# Listen to the mavlink messages that will be used as trigger to set EKF home automatically
if auto_set_ekf_home_enable == True:
    vehicle.add_message_listener('STATUSTEXT', statustext_callback)

if compass_enabled == 1:
    # Listen to the attitude data in aeronautical frame
    vehicle.add_message_listener('ATTITUDE', att_msg_callback)

data = None
current_confidence = None
H_aeroRef_aeroBody = None
heading_north_yaw = None

# Send MAVlink messages in the background
sched = BackgroundScheduler()

sched.add_job(send_vision_position_message, 'interval', seconds = 1/vision_msg_hz)
sched.add_job(send_confidence_level_dummy_message, 'interval', seconds = 1/confidence_msg_hz)

# For scale calibration, we will use a thread to monitor user input
if scale_calib_enable == True:
    scale_update_thread = threading.Thread(target=scale_update)
    scale_update_thread.daemon = True
    scale_update_thread.start()

sched.start()

if compass_enabled == 1:
    # Wait a short while for yaw to be correctly initiated
    time.sleep(1)

print("INFO: Sending VISION_POSITION_ESTIMATE messages to FCU.", flush=True)

counter = 0
try:
    while True:
        # Monitor last_heartbeat to reconnect in case of lost connection
        if vehicle.last_heartbeat > connection_timeout_sec_default:
            is_vehicle_connected = False
            print("WARNING: CONNECTION LOST. Last hearbeat was %f sec ago."% vehicle.last_heartbeat, flush=True)
            print("WARNING: Attempting to reconnect ...", flush=True)
            client.publish("status", "reconnecting vehicle")
            vehicle_connect()
            continue

        # Wait for the next set of frames from the camera
        frames = pipe.wait_for_frames()

        counter = counter + 1
        if counter % 10 == 0:
            print("counter: " + str(counter), flush=True)
            client.publish("status", "counter: " + str(counter))
            # fetch imagees
            f1 = frames.get_fisheye_frame(1)
            f2 = frames.get_fisheye_frame(2)
            send_image(f1, client, 'relay/stream/3')
        if counter == 10000:
            counter = 0

        # Fetch pose frame
        pose = frames.get_pose_frame()

        if pose:
            # Store the timestamp for MAVLink messages
            current_time = int(round(time.time() * 1000000))

            # Pose data consists of translation and rotation
            data = pose.get_pose_data()

            # In transformations, Quaternions w+ix+jy+kz are represented as [w, x, y, z]!
            H_T265Ref_T265body = tf.quaternion_matrix([data.rotation.w, data.rotation.x, data.rotation.y, data.rotation.z])
            H_T265Ref_T265body[0][3] = data.translation.x * scale_factor
            H_T265Ref_T265body[1][3] = data.translation.y * scale_factor
            H_T265Ref_T265body[2][3] = data.translation.z * scale_factor

            # Transform to aeronautic coordinates (body AND reference frame!)
            H_aeroRef_aeroBody = H_aeroRef_T265Ref.dot( H_T265Ref_T265body.dot( H_T265body_aeroBody))

            # Take offsets from body's center of gravity (or IMU) to camera's origin into account
            if body_offset_enabled == 1:
                H_body_camera = tf.euler_matrix(0, 0, 0, 'sxyz')
                H_body_camera[0][3] = body_offset_x
                H_body_camera[1][3] = body_offset_y
                H_body_camera[2][3] = body_offset_z
                H_camera_body = np.linalg.inv(H_body_camera)
                H_aeroRef_aeroBody = H_body_camera.dot(H_aeroRef_aeroBody.dot(H_camera_body))

            # Realign heading to face north using initial compass data
            if compass_enabled == 1:
                H_aeroRef_aeroBody = H_aeroRef_aeroBody.dot( tf.euler_matrix(0, 0, heading_north_yaw, 'sxyz'))

            # Show debug messages here
            if debug_enable == 1:
                os.system('clear') # This helps in displaying the messages to be more readable
                print("DEBUG: Raw RPY[deg]: {}".format( np.array( tf.euler_from_matrix( H_T265Ref_T265body, 'sxyz')) * 180 / m.pi), flush=True)
                print("DEBUG: NED RPY[deg]: {}".format( np.array( tf.euler_from_matrix( H_aeroRef_aeroBody, 'sxyz')) * 180 / m.pi), flush=True)
                print("DEBUG: Raw pos xyz : {}".format( np.array( [data.translation.x, data.translation.y, data.translation.z])), flush=True)
                print("DEBUG: NED pos xyz : {}".format( np.array( tf.translation_from_matrix( H_aeroRef_aeroBody))), flush=True)


except KeyboardInterrupt:
    print("INFO: KeyboardInterrupt has been caught. Cleaning up...", flush=True)

except Exception as err:
    print("Error: " + str(err), flush=True)

finally:
    pipe.stop()
    vehicle.close()
    client.loop_stop(force=True)
    print("INFO: Realsense pipeline and vehicle object closed.", flush=True)
    sys.exit(1)

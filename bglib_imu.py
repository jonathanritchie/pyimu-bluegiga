""" "IMU API script"

============================================
BGLib Python interface library code is placed under the MIT license
Copyright (c) 2014 Jeff Rowberg

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
===============================================

"""

__author__ = "Jonathan Ritchie"
__license__ = "MIT"
__version__ = "2016-08-10"
__email__ = "jrit927@aucklanduni.ac.nz"

"""
BASIC ARCHITECTURAL OVERVIEW:
    The program starts, initializes the dongle and then performs
    various tasks according to the command line arguments given.

    The basic default process is as follows:
      a. Scan for devices
      b. If the desired UUID is found in an ad packet, connect to that device
      c. Search for all "service" descriptors to find the target service handle range
      d. Search through the target service to find the IMU state attribute handle
      e. Enable notifications on the IMU state attribute
      f. Read and display incoming IMU values until terminating (Ctrl+C)

FUNCTION ANALYSIS:

1. __main__:
    Initializes the serial port and BGLib object to attach event handlers, sets
    global variables according to the users input and sends the appropriate commands
    to the device to start sampling from the connected IMU(s). Depending on the users
    input, this could send commands to cause the device to disconnect, stop advertising,
    and stop scanning (i.e. return to a known idle/standby state), or retain previous
    connections and start sampling from the already connected IMU(s). Some of
    these commands will fail since the device cannot be doing all of these
    things at the same time, but this is not a problem. __main__ finishes
    by setting scan parameters and initiating a scan with the "gap_discover"
    command. The full range of user inputs are described by using the --help
    option from the command line.

2. my_ble_evt_gap_scan_response:
    Raised during scanning whenever an advertisement packet is detected. The
    data provided includes the MAC address, RSSI, and ad packet data payload.
    This payload includes fields which contain any services being advertised,
    which allows us to scan for a specific service. The service
    we are searching for has a custom 128-bit UUID which is contained in the
    "uuid_imu_measurement_service" variable. Once a match is found, the script
    initiates a connection request with the "gap_connect_direct" command.

3. my_ble_evt_connection_status
    Raised when the connection status is updated. This happens when the
    connection is first established, and the "flags" byte will contain 0x05 in
    this instance. However, it will also happen if the connected devices bond
    (i.e. pair), or if encryption is enabled (e.g. with "sm_encrypt_start").
    Once a connection is established, the script begins a service discovery
    with the "attclient_read_by_group_type" command.

4. my_ble_evt_attclient_group_found
    Raised for each group found during the search started in #3. If the right
    service is found (matched by UUID), then its start/end handle values are
    stored for usage later. We cannot use them immediately because the ongoing
    read-by-group-type procedure must finish first.

5. my_ble_evt_attclient_find_information_found
    Raised for each attribute found during the search started after the service
    search completes. We look for two specific attributes during this process;
    the first is the unique IMU state attribute which has a proprietary
    128-bit UUID (contained in the "uuid_IMU_state_characteristic"
    variable), and the second is the corresponding "client characteristic
    configuration" attribute with a UUID of 0x2902. The correct attribute here
    will always be the first 0x2902 attribute after the measurement attribute
    in question. Typically the CCC handle value will be either +1 or +2 from
    the original handle. The attribute handles discovered during this step
    are all stored for later use in the script in the 'characteristic_handles'
    dictionary object.

6. my_ble_evt_attclient_procedure_completed
    Raised when an attribute client procedure finishes, which in this script
    means when the "attclient_read_by_group_type" (service search) or the
    "attclient_find_information" (descriptor search) completes. Since both
    processes terminate with this same event, we must keep track of the state
    so we know which one has actually just finished. The completion of the
    service search will (assuming the service is found) trigger the start of
    the descriptor search, and the completion of the descriptor search will
    (assuming the attributes are found) trigger enabling notifications on
    the measurement characteristic. This event is also raised anytime an
    'attribute_write' command finishes executing.

7. my_ble_evt_attclient_attribute_value
    Raised each time the remote device pushes new data via notifications or
    indications. (Notifications and indications are basically the same, except
    that indications are acknowledged while notifications are not--like TCP vs.
    UDP.) In this script, the remote slave device pushes IMU measurements out
    as fast as possible, and the user can choose to store or display these
    sample packets by pressing ENTER. This event is also raised anytime an attribute
    value is read using the 'read_by_handle' command.
    
8. my_ble_rsp_attclient_attribute_write
    This response is recieved when an 'attribute_write' command is sent to an IMU.
    The information recieved in this callback indicates if the command was received
    successfully or not by the peripheral device.

9. my_ble_rsp_attclient_read_by_handle
    This response is recieved when an 'read_by_handle' command is sent to an IMU.
    The information recieved in this callback indicates if the command was received
    successfully or not by the peripheral device.

10. my_ble_rsp_connection_get_rssi
    This response is received when a 'connection_get_rssi' command is sent to an
    IMU. The information received in this callback indicates whether the IMU is
    still connected to the dongle by giving a negative RSSI value, or if the IMU
    is no longer connected to the dongle by giving a zero RSSI value. This can be
    used to determine if a previously active connection is still active after the
    previous script run was terminated.

"""

import bglib, serial, time, threading, optparse, signal, json, math, Queue

# define global variables for sampling settings, connection information, and data conversions
ble = 0
ser = 0
more_output = False
store_data = False
sleep_time = None
no_sample = False
file_handle = None
sensor_data_queue = None
configuring_sensors = False
finished_sampling = False
returned_to_idle = False
gyro_divisor = (math.pi/180)/16.4
high_freq = False
stop_sampling_time = 0
current_connection_handle = 0
number_of_imus = 0
remaining_connections = 0
sample_attribute_ccc = 0
peripheral_list = []
connection_handles = []
previous_peripheral_list = []
previous_connection_handles = []
sampling_options = {"low":2, "high":4}
sample_start = None
sample_running = None
characteristics_handles = {'sample':0, 'state':0, 'timestamp':0, 'address':0, 'battery_level':0, \
                           'manufacturer_name':0, 'model_number':0, 'serial_number':0, \
                           'hardware_revision':0, 'firmware_revision':0}

# define all required uuid's for the IMU
uuid_service = [0x28, 0x00] # 0x2800
uuid_client_characteristic_configuration = [0x29, 0x02] # 0x2902

uuid_IMU_measurement_service = [0x1B, 0xC5, 0x00, 0x01, 0x02, 0x00, 0xFD, 0xBD, 0xE4, 0x11, 0x01, \
                                0x48, 0xE0, 0x2A, 0xF8, 0x27]
uuid_IMU_sample_characteristic = [0x1B, 0xC5, 0x00, 0x02, 0x02, 0x00, 0xFD, 0xBD, 0xE4, 0x11, 0x01, \
                                  0x48, 0xE0, 0x2A, 0xF8, 0x27]
uuid_IMU_state_characteristic = [0x1B, 0xC5, 0x00, 0x03, 0x02, 0x00, 0xFD, 0xBD, 0xE4, 0x11, 0x01, \
                                 0x48, 0xE0, 0x2A, 0xF8, 0x27]
uuid_IMU_timestamp_characteristic = [0x1B, 0xC5, 0x00, 0x04, 0x02, 0x00, 0xFD, 0xBD, 0xE4, 0x11, \
                                     0x01, 0x48, 0xE0, 0x2A, 0xF8, 0x27]
uuid_IMU_address_characteristic = [0x1B, 0xC5, 0x00, 0x05, 0x02, 0x00, 0xFD, 0xBD, 0xE4, 0x11, 0x01, \
                                   0x48, 0xE0, 0x2A, 0xF8, 0x27]
uuid_IMU_battery_service = [0x18, 0x0F]
uuid_IMU_battery_level_characteristic = [0x2A, 0x19]
uuid_IMU_device_information_service = [0x18, 0x0A]
uuid_IMU_manufacturer_name_characteristic = [0x2A, 0x29]
uuid_IMU_model_number_characteristic = [0x2A, 0x24]
uuid_IMU_serial_number_characteristic = [0x2A, 0x25]
uuid_IMU_hardware_revision_characteristic = [0x2A, 0x27]
uuid_IMU_firmware_revision_characteristic = [0x2A, 0x26]
service_handles = {'imu_service':[0, 0], 'battery_service':[0, 0], 'information_service':[0, 0]}

# define the different states that our program can be in (used for program execution control)
STATE_STANDBY = 0
STATE_CONNECTING = 1
STATE_FINDING_SERVICES = 2
STATE_FINDING_SERVICE_ATTRIBUTES = 3
STATE_SAMPLING = 4
STATE_READING_BATTERY_LEVEL = 5
STATE_FINDING_BATTERY_SERVICE_ATTRIBUTES = 6
STATE_FINDING_INFORMATION_SERVICE_ATTRIBUTES = 7 
STATE_CHECK_SENSOR_IDLE = 8
STATE_START_SAMPLING = 9
STATE_CHECK_SAMPLING_RUNNING = 10
STATE_ENABLE_NOTIFICATIONS = 11
STATE_POST_SAMPLING_CHECK = 12
STATE_DISABLE_NOTIFICATIONS = 13
STATE_RECORDING_SAMPLES = 14
STATE_STOP_SAMPLING_CONFIRM = 15
STATE_RECONNECTING_SENSOR = 16
STATE_WAIT_START_SAMPLING = 17
STATE_DISPLAYING_PACKETS = 18
STATE_STOP_SAMPLING_CHECK_RUNNING = 19
STATE_FINISHED_SAMPLING = 20
state = STATE_STANDBY

# handler to notify of an API parser timeout condition
def my_timeout(sender, args):
    # might want to try the following lines to reset, though it probably
    # wouldn't work at this point if it's already timed out:
    #ble.send_command(ser, ble.ble_cmd_system_reset(0))
    #ble.check_activity(ser, 1)
    print "BGAPI parser timed out. Make sure the BLE device is in a known/idle state."

# gap_scan_response handler
def my_ble_evt_gap_scan_response(sender, args):
    global state, ble, ser, uuid_IMU_measurement_service, number_of_imus, peripheral_list

    # pull all advertised service info from ad packet
    ad_services = []
    this_field = []
    bytes_left = 0
    for b in args['data']:
        if bytes_left == 0:
            bytes_left = b
            this_field = []
        else:
            this_field.append(b)
            bytes_left = bytes_left - 1
            if bytes_left == 0:
                if this_field[0] == 0x02 or this_field[0] == 0x03: # partial or complete list of 16-bit UUIDs
                    for i in xrange((len(this_field) - 1) / 2):
                        ad_services.append(this_field[-1 - i*2 : -3 - i*2 : -1])
                if this_field[0] == 0x04 or this_field[0] == 0x05: # partial or complete list of 32-bit UUIDs
                    for i in xrange((len(this_field) - 1) / 4):
                        ad_services.append(this_field[-1 - i*4 : -5 - i*4 : -1])
                if this_field[0] == 0x06 or this_field[0] == 0x07: # partial or complete list of 128-bit UUIDs
                    for i in xrange((len(this_field) - 1) / 16):
                        ad_services.append(this_field[-1 - i*16 : -17 - i*16 : -1])

    # check for 1BC500010200FDBDE4110148E02AF827 (custom "IMU service" service UUID)
    # NOTE: ad_services is built in normal byte order, reversed from that reported in the ad packet

    if uuid_IMU_measurement_service in ad_services:
        if (not(args['sender'] in peripheral_list)) and (len(peripheral_list) < number_of_imus):
            peripheral_list.append(args['sender'])
            #print "%s" % ':'.join(['%02X' % b for b in args['sender'][::-1]])

            # connect to this device using very fast connection parameters (7.5ms - 15ms range)
            ble.send_command(ser, ble.ble_cmd_gap_connect_direct(args['sender'], args['address_type'], \
                                                                 0x06, 0x0C, 0x100, 0))
            ble.check_activity(ser, 1)
            state = STATE_CONNECTING

# connection_status handler
def my_ble_evt_connection_status(sender, args):
    global state, ble, ser, connection_handles, remaining_connections, peripheral_list, uuid_service

    if (args['flags'] & 0x05) == 0x05:
        # connected, now perform service discovery
        print "Connected to %s" % ':'.join(['%02X' % b for b in args['address'][::-1]])
        connection_handles.append(args['connection'])
        if not (args['address'] in peripheral_list):
            peripheral_list.append(args['address'])
        remaining_connections -= 1
        
        if state != STATE_RECONNECTING_SENSOR:
            
            # NOTE: BGLib command expects little-endian UUID byte order, so it must be reversed for using
            # NOTE2: must be put inside "list()" so that it is once again iterable
            state = STATE_FINDING_SERVICES
            ble.send_command(ser, ble.ble_cmd_attclient_read_by_group_type(args['connection'], 0x0001, \
                                                                           0xFFFF, list(reversed(uuid_service))))
            ble.check_activity(ser, 1)

# attclient_group_found handler
def my_ble_evt_attclient_group_found(sender, args):
    global uuid_IMU_measurement_service, uuid_IMU_battery_service, uuid_IMU_device_information_service, \
    service_handles, more_output

    # found "service" attribute groups (UUID=0x2800), check for IMU service (0x 1BC500010200FDBDE4110148E02AF827)
    # NOTE: args['uuid'] contains little-endian UUID byte order directly from the API response, so it must be reversed for comparison
    if args['uuid'] == list(reversed(uuid_IMU_measurement_service)):
        if more_output: print "Found attribute group for IMU service: start=%d, end=%d" % (args['start'], \
                                                                                              args['end'])
        service_handles['imu_service'] = [args['start'], args['end']]
    elif args['uuid'] == list(reversed(uuid_IMU_battery_service)):
        if more_output: print "Found attribute group for IMU battery service: start=%d, end=%d" \
                %(args['start'], args['end'])
        service_handles['battery_service'] = [args['start'], args['end']]
    elif args['uuid'] == list(reversed(uuid_IMU_device_information_service)):
        if more_output: print "Found attribute group for IMU device information service: start=%d, end=%d" \
                %(args['start'], args['end'])
        service_handles['information_service'] = [args['start'], args['end']]

# attclient_find_information_found handler
def my_ble_evt_attclient_find_information_found(sender, args):
    global uuid_IMU_sample_characteristic, uuid_IMU_address_characteristic, uuid_IMU_battery_level_characteristic, \
        uuid_IMU_firmware_revision_characteristic, uuid_IMU_hardware_revision_characteristic, \
        uuid_IMU_manufacturer_name_characteristic, uuid_IMU_model_number_characteristic, uuid_IMU_state_characteristic, \
        uuid_IMU_timestamp_characteristic, characteristics_handles, uuid_IMU_serial_number_characteristic, more_output, \
        sample_attribute_ccc

    # store all appropriate characteristic handles for the IMU
    # NOTE: args['uuid'] contains little-endian UUID byte order directly from the API response, so it must be reversed for comparison
    if args['uuid'] == list(reversed(uuid_IMU_sample_characteristic)):
        characteristics_handles['sample'] = args['chrhandle']
    elif args['uuid'] == list(reversed(uuid_IMU_state_characteristic)):
        characteristics_handles['state'] = args['chrhandle']
    elif args['uuid'] == list(reversed(uuid_IMU_timestamp_characteristic)):
        characteristics_handles['timestamp'] = args['chrhandle']
    elif args['uuid'] == list(reversed(uuid_IMU_address_characteristic)):
        characteristics_handles['address'] = args['chrhandle']
    elif args['uuid'] == list(reversed(uuid_IMU_battery_level_characteristic)):
        characteristics_handles['battery_level'] = args['chrhandle']
    elif args['uuid'] == list(reversed(uuid_IMU_manufacturer_name_characteristic)):
        characteristics_handles['manufacturer_name'] = args['chrhandle']
    elif args['uuid'] == list(reversed(uuid_IMU_model_number_characteristic)):
        characteristics_handles['model_number'] = args['chrhandle']
    elif args['uuid'] == list(reversed(uuid_IMU_serial_number_characteristic)):
        characteristics_handles['serial_number'] = args['chrhandle']
    elif args['uuid'] == list(reversed(uuid_IMU_hardware_revision_characteristic)):
        characteristics_handles['hardware_revision'] = args['chrhandle']
    elif args['uuid'] == list(reversed(uuid_IMU_firmware_revision_characteristic)):
        characteristics_handles['firmware_revision'] = args['chrhandle']
    
    # discover client characteristic configuration
    elif (args['uuid'] == list(reversed(uuid_client_characteristic_configuration))) and \
            ((characteristics_handles['sample'] > 0) and (sample_attribute_ccc == 0)):
        if more_output: print "Found characteristic configuration: handle=%d" % args['chrhandle']
        sample_attribute_ccc = args['chrhandle']
    
    # Print info to command line
    if (args['chrhandle'] in characteristics_handles.values()) and more_output:
        print "Found attribute for %s: handle=%d" %(characteristics_handles.keys()[characteristics_handles.values().index(args['chrhandle'])], args['chrhandle'])

# attclient_procedure_completed handler
def my_ble_evt_attclient_procedure_completed(sender, args):
    global state, ble, ser, service_handles, characteristics_handles, store_data, more_output, \
        current_connection_handle, number_of_imus, connection_handles

    # check if we just finished searching for services
    if state == STATE_FINDING_SERVICES:
        if service_handles['information_service'][1] > 0:
            if more_output: print "Found IMU services"

            # found the IMU service, so now search for the attributes inside
            state = STATE_FINDING_SERVICE_ATTRIBUTES
            ble.send_command(ser, ble.ble_cmd_attclient_find_information(args['connection'], service_handles['imu_service'][0], service_handles['imu_service'][1]))
            ble.check_activity(ser, 1)
        else:
            if more_output: print "Could not find IMU services"

    # check if we just finished searching for attributes within the IMU service
    elif state == STATE_FINDING_SERVICE_ATTRIBUTES:
            
        # found the IMU service attributes, so now search for the battery service attributes 
        state = STATE_FINDING_BATTERY_SERVICE_ATTRIBUTES
        ble.send_command(ser, ble.ble_cmd_attclient_find_information(args['connection'], service_handles['battery_service'][0], service_handles['battery_service'][1]))
        ble.check_activity(ser, 1)
        
    elif state == STATE_FINDING_BATTERY_SERVICE_ATTRIBUTES:
        
        # found the IMU battery service attributes, so now search for the information service attributes
        state = STATE_FINDING_INFORMATION_SERVICE_ATTRIBUTES
        ble.send_command(ser, ble.ble_cmd_attclient_find_information(args['connection'], service_handles['information_service'][0], service_handles['information_service'][1]))
        ble.check_activity(ser, 1)
        
    elif state == STATE_FINDING_INFORMATION_SERVICE_ATTRIBUTES:
        if more_output:  print "Found all IMU attributes"
        
        # read the battery level of the IMU
        print('Battery Level:')
        state = STATE_READING_BATTERY_LEVEL
        ble.send_command(ser, ble.ble_cmd_attclient_read_by_handle(args['connection'], characteristics_handles['battery_level']))
        ble.check_activity(ser, 1)
    
    # enable notifications on the 'sample' attribute for each connected IMU
    elif state == STATE_ENABLE_NOTIFICATIONS:
        if args['result'] == 0:
            if more_output: print("Notifications for IMU %d enabled successfully" %(current_connection_handle + 1))
            
            if (current_connection_handle + 1) < number_of_imus:
                current_connection_handle += 1
                ble.send_command(ser, ble.ble_cmd_attclient_attribute_write(connection_handles[current_connection_handle], sample_attribute_ccc, [0x01, 0x00]))
                ble.check_activity(ser, 1)
            else:
                current_connection_handle = 0 
                state = STATE_SAMPLING
                
                # start user input thread to start sampling
                user_input_start_capturing_thread = threading.Thread(target=user_input_background)
                user_input_start_capturing_thread.daemon = True
                user_input_start_capturing_thread.start()
                
                if store_data:
                    print("IMU sensor(s) sampling. Press ENTER to record samples:  ")
                else:
                    print("IMU sensor(s) sampling. Press ENTER to display received packets:  ")
        else:
            if more_output: print("Notifications not enabled successfully, trying again...")
            ble.send_command(ser, ble.ble_cmd_attclient_attribute_write(args['connection'], sample_attribute_ccc, [0x01, 0x00]))
            ble.check_activity(ser, 1)

    # once the start_sampling command has been sent to the IMU we need to check that the IMU has transistioned to the sampling_running state
    elif state == STATE_START_SAMPLING:
        if args['result'] == 0:
            if more_output: print("Start sampling state written successfully, checking IMU is sampling now...")
            state = STATE_CHECK_SAMPLING_RUNNING
            ble.send_command(ser, ble.ble_cmd_attclient_read_by_handle(args['connection'], characteristics_handles['state']))
            ble.check_activity(ser, 1)
        else:
            if more_output: print("Start sampling state not written successfully, trying again...")
            ble.send_command(ser, ble.ble_cmd_attclient_attribute_write(args['connection'], characteristics_handles['state'], [sample_start]))
            ble.check_activity(ser, 1)
            
    # once the stop command has been sent to the IMU we need to check that the IMU has returned to the idle state
    elif state == STATE_STOP_SAMPLING_CONFIRM:
        if args['result'] == 0:
            state = STATE_CHECK_SENSOR_IDLE
            if more_output: print("Stop sampling state written successfully, checking IMU is IDLE now...")
            ble.send_command(ser, ble.ble_cmd_attclient_read_by_handle(args['connection'], characteristics_handles['state']))
        else:
            if more_output: print("Stop sampling state not written successfully, trying again...")
            ble.send_command(ser, ble.ble_cmd_attclient_attribute_write(args['connection'], characteristics_handles['state'], [22]))
            ble.check_activity(ser, 1)
            
    # before stopping sampling on each IMU we need to disable notifications from each connected device - this is so that the evt_attribute_value
    # callback does not continue to be called while trying to send commands and receive other packets of information 
    elif state == STATE_DISABLE_NOTIFICATIONS:
        if args['result'] == 0:
            if (current_connection_handle + 1) < number_of_imus:
                current_connection_handle += 1
                ble.send_command(ser, ble.ble_cmd_attclient_attribute_write(connection_handles[current_connection_handle], sample_attribute_ccc, [0x00, 0x00]))
                ble.check_activity(ser, 1)
            else:
                current_connection_handle = 0
                state = STATE_STOP_SAMPLING_CHECK_RUNNING
                if more_output: print("Notifications diabled successfully, stopping sampling now...")
                ble.send_command(ser, ble.ble_cmd_attclient_read_by_handle(connection_handles[current_connection_handle], characteristics_handles['state']))
                ble.check_activity(ser, 1)
        
def my_ble_rsp_attclient_read_by_handle(sender, args):
    global ble, ser, state, characteristics_handles, more_output
    
    # check if the read_by_handle commands have been received successfully by the IMU. If not, try sending the command again until it is 
    # received successfully
    if state == STATE_CHECK_SENSOR_IDLE or state == STATE_CHECK_SAMPLING_RUNNING or state == STATE_STOP_SAMPLING_CHECK_RUNNING or state == STATE_READING_BATTERY_LEVEL:
        if args["result"] == 0:
            if more_output: print("Sensor attribute read successful")
        else:
            if more_output: print("Sensor attribute not read successfully, trying again...")
            if state == STATE_READING_BATTERY_LEVEL:
                ble.send_command(ser, ble.ble_cmd_attclient_read_by_handle(args['connection'], characteristics_handles['battery_level']))
            else:
                ble.send_command(ser, ble.ble_cmd_attclient_read_by_handle(args['connection'], characteristics_handles['state']))
            ble.check_activity(ser, 1)

# attclient_attribute_value handler
def my_ble_evt_attclient_attribute_value(sender, args):
    global state, ble, ser, remaining_connections, characteristics_handles, sample_attribute_ccc, \
        sample_running, no_sample, store_data, finished_sampling, more_output, configuring_sensors, \
        current_connection_handle, connection_handles, sensor_data_queue, returned_to_idle
             
    # called when a sample packet is pushed from each device to the dongle
    if state == STATE_RECORDING_SAMPLES:

        # add sample data to queue
        sensor_data_queue.put([time.time(), args['connection'], args['value']])
                
    elif state == STATE_FINISHED_SAMPLING:
            # if we have finished sampling then we need to disable notifications and stop sampling from each sensor
            state = STATE_DISABLE_NOTIFICATIONS
            ble.send_command(ser, ble.ble_cmd_attclient_attribute_write(connection_handles[current_connection_handle], sample_attribute_ccc, [0x00, 0x00]))
            ble.check_activity(ser, 1)
            
    # display recieved packets on the command line
    elif state == STATE_DISPLAYING_PACKETS:
        print(args['value'])
            
    # print battery level to user
    elif state == STATE_READING_BATTERY_LEVEL:
        print(str(args['value'][0]) + '%\n')
        
        # if we still have connect to more IMUs then continue connecting
        if remaining_connections:
            state = STATE_STANDBY
            print("Scanning for remaining IMUs...")
            ble.send_command(ser, ble.ble_cmd_gap_discover(1))
            ble.check_activity(ser, 1)
        elif no_sample:
            # free up bandwidth
            turn_off_scan()
            
            # start user input thread to start sampling
            state = STATE_WAIT_START_SAMPLING
            print("IMU sensor(s) ready to start sampling. Press ENTER to start sampling:  ")
            user_input_start_sampling_thread = threading.Thread(target=user_input_background)
            user_input_start_sampling_thread.daemon = True
            user_input_start_sampling_thread.start()
        else:
            # free up bandwidth
            turn_off_scan()
            
            # send commands to the connected IMU(s) to start sampling and enable notifications
            configuring_sensors = True
            
            # before sending commands to the IMU(s) we need to check that each device is in the IDLE state
            state = STATE_CHECK_SENSOR_IDLE
            ble.send_command(ser, ble.ble_cmd_attclient_read_by_handle(connection_handles[current_connection_handle], characteristics_handles['state']))
            ble.check_activity(ser, 1)
            
    # if the IMU is in the IDLE state then we can continue to send the start_sampling command to the device according to the user's input
    elif state == STATE_CHECK_SENSOR_IDLE:
        if not finished_sampling:
            if args['value'][0] == 1:
                if more_output: print("IMU sensor is in IDLE state, starting sampling now...")
                state = STATE_START_SAMPLING
                ble.send_command(ser, ble.ble_cmd_attclient_attribute_write(args['connection'], args['atthandle'], [sample_start]))
                ble.check_activity(ser, 1)
            else:
                if more_output: print("IMU sensor is in state: %d, not in IDLE state, checking state again..." %(args['value'][0]))
                ble.send_command(ser, ble.ble_cmd_attclient_read_by_handle(args['connection'], args['atthandle']))
                ble.check_activity(ser, 1)
        else:
            # checking that the IMU has returned to the IDLE state after sampling has stopped
            if args['value'][0] == 1:
                print("IMU %d has returned to IDLE state." %(args['connection'] + 1))
            if (current_connection_handle + 1) < number_of_imus:
                current_connection_handle += 1
                state = STATE_STOP_SAMPLING_CHECK_RUNNING
                ble.send_command(ser, ble.ble_cmd_attclient_read_by_handle(connection_handles[current_connection_handle], characteristics_handles['state']))
                ble.check_activity(ser, 1)
            else:
                current_connection_handle = 0
                returned_to_idle = True
                if store_data:
                    file_write_sample_data()
                
                # store connection handles and bluetooth addresses of connected IMUs
                with open('connections.txt', 'w') as outfile:
                    json.dump([peripheral_list, connection_handles, characteristics_handles, sample_attribute_ccc], outfile)
            
    # after sending the start_sampling command to the IMU we need to check that it has transitioned to the sampling_running state
    elif state == STATE_CHECK_SAMPLING_RUNNING:
        if args['value'][0] == sample_running:
            # repeat process for each connected IMU
            if configuring_sensors and ((current_connection_handle + 1) < number_of_imus):
                if more_output: print("IMU %d is sampling" %(current_connection_handle + 1))
                state = STATE_CHECK_SENSOR_IDLE
                current_connection_handle += 1
                ble.send_command(ser, ble.ble_cmd_attclient_read_by_handle(connection_handles[current_connection_handle], args['atthandle']))
                ble.check_activity(ser, 1)
            else:
                # once all of the connected IMUs have started sampling, we can enable notifications for the sample attribute on each device
                configuring_sensors = False
                current_connection_handle = 0
                if more_output: print("IMU sensor(s) sampling, enabling notifications now...")
                state = STATE_ENABLE_NOTIFICATIONS
                ble.send_command(ser, ble.ble_cmd_attclient_attribute_write(connection_handles[current_connection_handle], sample_attribute_ccc, [0x01, 0x00]))
                ble.check_activity(ser, 1)
            
    # before sending a stop command to the IMU we need to check that the IMU is actually sampling
    elif state == STATE_STOP_SAMPLING_CHECK_RUNNING:
        if args['value'][0] == sample_running:
            if more_output: print("IMU sensor is sampling, stopping sampling now...")
            state = STATE_STOP_SAMPLING_CONFIRM
            ble.send_command(ser, ble.ble_cmd_attclient_attribute_write(args['connection'], characteristics_handles['state'], [22]))
            ble.check_activity(ser, 1)
        else:
            # even is the IMU isn't sampling we will send a stop command just to make sure that the device returns to the IDLE state
            if more_output: print("IMU sensor is not sampling and is in state: %d, trying to stop sensor anyway..." %(args['value'][0]))
            state = STATE_STOP_SAMPLING_CONFIRM
            ble.send_command(ser, ble.ble_cmd_attclient_attribute_write(args['connection'], characteristics_handles['state'], [22]))
            ble.check_activity(ser, 1)

def my_ble_rsp_attclient_attribute_write(sender, args):
    global state, ble, ser, characteristics_handles, sample_start, sample_attribute_ccc, more_output
    
    # check that all attribute_write commands sent to the IMU are recieved successfully, if not then try again until it is
    if state == STATE_START_SAMPLING or state == STATE_ENABLE_NOTIFICATIONS or state == STATE_STOP_SAMPLING_CONFIRM or state == STATE_DISABLE_NOTIFICATIONS:
        if (args['result'] == 0):
            if more_output: print("Attribute write command successful, waiting for procedure to complete...")
        else:
            if more_output: print("Attribute write command not successful, trying again...")
            if state == STATE_START_SAMPLING:
                ble.send_command(ser, ble.ble_cmd_attclient_attribute_write(args['connection'], characteristics_handles['state'], [sample_start]))
            elif state == STATE_ENABLE_NOTIFICATIONS:
                ble.send_command(ser, ble.ble_cmd_attclient_attribute_write(args['connection'], sample_attribute_ccc, [0x01, 0x00]))
            elif state == STATE_STOP_SAMPLING_CONFIRM:
                ble.send_command(ser, ble.ble_cmd_attclient_attribute_write(args['connection'], characteristics_handles['state'], [22]))
            else:
                ble.send_command(ser, ble.ble_cmd_attclient_attribute_write(args['connection'], sample_attribute_ccc, [0x00, 0x00]))
            ble.check_activity(ser, 1)
        
def my_ble_rsp_connection_get_rssi(sender, args):
    global state, ble, ser, connection_handles, peripheral_list, previous_connection_handles, previous_peripheral_list, remaining_connections
    
    # check if the previously connected IMU is still connected to the dongle by inspecting the received rssi value
    handle_index = previous_connection_handles.index(args['connection'])
    if (args['rssi'] == 0):
        print("Previous connection no longer present. Attempting to reconnect...")
        state = STATE_RECONNECTING_SENSOR
        ble.send_command(ser, ble.ble_cmd_gap_connect_direct(previous_peripheral_list[handle_index], 1, 0x06, 0x0C, 0x100, 0))
        ble.check_activity(ser, 1)
    else:
        connection_handles.append(args['connection'])
        peripheral_list.append(previous_peripheral_list[handle_index])
        remaining_connections -= 1
        
# this function takes an unsigned integer of 'bits' number of bits and returns the 2's compliment value for the input bit sequence
def twos_comp_16_bits(data):
    val = (data[0] << 8) + data[1]
    # compute the two's compliment of the input value (unsigned int)
    if (val & (1 << 15)) != 0: # if sign bit is set e.g., 8bit: 128-255
        val = val - (1 << 16)        # compute negative value
    return val
    
def interpret_data(data):
    convert = twos_comp_16_bits
    data = [[data[0], data[1]], [data[2], data[3]], [data[4], data[5]], [data[6], data[7]], [data[8], data[9]], [data[10], data[11]], \
            [data[12], data[13]], [data[14], data[15]], [data[16], data[17]]]
    return map(convert, data)

def main():
    global ble, ser, number_of_imus, remaining_connections, previous_peripheral_list, previous_connection_handles, \
        minimal_output, store_data, file_handle,high_freq, sample_running, sample_start, sampling_options, \
        characteristics_handles, sample_attribute_ccc, state, no_sample, more_output, sensor_data_queue, \
        returned_to_idle, peripheral_list, sleep_time

    # create option parser
    p = optparse.OptionParser(description='IMU Collector v' + __version__)

    # set defaults for options
    p.set_defaults(port="/dev/tty.usbmodem1", baud=115200, packet=False, sample_mode=False, debug=False, connect=1, leave=False, more_output=False, highFreq=False, no_sample=False, store=False, filename='imu_data.csv')

    # create serial port options argument groups
    group = optparse.OptionGroup(p, "Connection Options")
    group.add_option('--port', '-p', type="string", help="Serial port device name (default /dev/usbmodem1)", metavar="PORT")
    group.add_option('--baud', '-b', type="int", help="Serial port baud rate (default 115200)", metavar="BAUD")
    group.add_option('--packet', '-k', action="store_true", help="Packet mode (prefix API packets with <length> byte)")
    group.add_option('--sample_mode', '-m', action="store_true", help="Use this option if you are already connected to IMU(s) and you wish to start sampling")
    group.add_option('--debug', '-d', action="store_true", help="Debug mode (show raw RX/TX API packets)")
    group.add_option('--connect', '-c', action="store", type="int", help="Number of IMU sensors to connect to (default is 1, maximum is 6)")
    group.add_option('--leave', '-l', action="store_true", help="Leave existing connections intact, do not disconnect")
    group.add_option('--more_output', '-o', action="store_true", help="Use this option to print more detailed information to the command line")
    p.add_option_group(group)
    
    group = optparse.OptionGroup(p, "Sampling Options", "Please read the IMU developer documentation for sampling details.")
    group.add_option('--high_freq', '-q', action="store_true", help="Use this option to sample from the IMU(s) at a higher frequency. Low frequency sampling uses 9 axes and high frequency sampling uses 3 axes. By default we sample at low frequency.")
    group.add_option('--no_sample', '-n', action='store_true', help="Use this option to prevent the IMU(s) from starting sampling automatically")
    p.add_option_group(group)
    
    group = optparse.OptionGroup(p, "Storage Options")
    group.add_option('--store', '-s', action="store_true", help="Use this option to store sampled data from the IMU(s) to a file. By default sample measurements will be printed to the command line.")
    group.add_option('--file', '-f', dest="filename", help="Write measurements to FILE", metavar="FILE")
    p.add_option_group(group)

    # actually parse all of the arguments
    options, _ = p.parse_args()

    # create and setup BGLib object
    ble = bglib.BGLib()
    ble.packet_mode = options.packet
    ble.debug = options.debug

    # add handler for BGAPI timeout condition (hopefully won't happen)
    ble.on_timeout += my_timeout

    # add handlers for BGAPI events
    ble.ble_evt_gap_scan_response += my_ble_evt_gap_scan_response
    ble.ble_evt_connection_status += my_ble_evt_connection_status
    ble.ble_evt_attclient_group_found += my_ble_evt_attclient_group_found
    ble.ble_evt_attclient_find_information_found += my_ble_evt_attclient_find_information_found
    ble.ble_evt_attclient_procedure_completed += my_ble_evt_attclient_procedure_completed
    ble.ble_evt_attclient_attribute_value += my_ble_evt_attclient_attribute_value
    ble.ble_rsp_connection_get_rssi += my_ble_rsp_connection_get_rssi
    ble.ble_rsp_attclient_attribute_write += my_ble_rsp_attclient_attribute_write
    ble.ble_rsp_attclient_read_by_handle += my_ble_rsp_attclient_read_by_handle
    
    # test for valid input options and set global variables
    number_of_imus = remaining_connections = options.connect
    more_output = options.more_output
    store_data = options.store
    high_freq = options.high_freq
    no_sample = options.no_sample
    baud_rate = options.baud
    
        # set state values for sampling
    if high_freq:
        sample_start = sampling_options['high']
        number_of_samples_per_second = 500
    else:
        sample_start = sampling_options['low']
        number_of_samples_per_second = 100
        
    sample_running = sample_start + 1
    
    # calculate appropriate baud rate and CPU sleep times based on maximum throughput rate from sensors
    # [no_of_bits_in_byte*no_of_bytes_in_sample_packet*number_of_packets_per_second*number_of_sensors]
    baud_rate = 8*27*number_of_samples_per_second*options.connect*2
    sleep_time = 0.001/number_of_imus

    # create serial port object
    try:
        ser = serial.Serial(port=options.port, baudrate=baud_rate, timeout=1, writeTimeout=1)
    except serial.SerialException as e:
        print "\n================================================================"
        print "Port error (name='%s', baud='%ld'): %s" % (options.port, options.baud, e)
        print "================================================================"
        exit(2)
        
    # flush buffers
    ser.flushInput()
    ser.flushOutput()
    
    # create queue for storing sample data in memory
    sensor_data_queue = Queue.Queue()
    
    # open file for storing sampled data
    if store_data:
        file_handle = open(options.filename, 'w')
        if high_freq:       
            file_handle.write('sensor_no,timestamp,packet_counter,ax,ay,az\n')
        else:
            file_handle.write('sensor_no,timestamp,packet_counter,ax,ay,az,gx,gy,gz,mx,my,mz\n')
            
    # recovery connection handles and bluetooth addresses from existing connections (if there are any)
    with open('connections.txt', 'r') as infile:
        connections_info = json.load(infile)
        
        if len(connections_info) > 0:
            previous_peripheral_list = connections_info[0]
            previous_connection_handles = connections_info[1]
            characteristics_handles = connections_info[2]
            sample_attribute_ccc = connections_info[3]
    
    # stop advertising if we are advertising already
    ble.send_command(ser, ble.ble_cmd_gap_set_mode(0, 0))
    ble.check_activity(ser, 1)
    
    # test if we are still connected to the sensor(s) when --sample_mode option is given
    # throw error message if there are no previous connections or if there is a size mismatch 
    # between the address and handle list
    if options.sample_mode:
        
        if ((len(previous_peripheral_list) == 0) or (len(previous_connection_handles) == 0)) \
            or (len(previous_peripheral_list) != len(previous_connection_handles)):
            print "There are no previous valid connections, please reconnect without --sample_mode"
            
        else:
            # test if our previous connections are still up
            test_previous_connections()
            
            # adjust number_of_imus to correct for existing connections
            number_of_imus = len(peripheral_list)
                
            # put dongle in known state
            turn_off_scan()
            
            # start sampling from IMU(s)
            state = STATE_CHECK_SENSOR_IDLE
            ble.send_command(ser, ble.ble_cmd_attclient_read_by_handle(connection_handles[0], characteristics_handles['state']))
            ble.check_activity(ser, 1) 

    else:
        if not options.leave:            
            
            # disconnect if we are connected already
            for connection_handle in previous_connection_handles:
                ble.send_command(ser, ble.ble_cmd_connection_disconnect(connection_handle))
                ble.check_activity(ser, 1)
        else:
            # test if our previous connections are still up
            test_previous_connections()
            
            # adjust number_of_imus and remaining_connections to correct for existing connections
            number_of_imus = len(peripheral_list) + options.connect
            remaining_connections = options.connect
       
        # set scan parameters
        ble.send_command(ser, ble.ble_cmd_gap_set_scan_parameters(0xC8, 0xC8, 1))
        ble.check_activity(ser, 1)
       
        # start scanning now
        print "Scanning for BLE peripherals..."
        ble.send_command(ser, ble.ble_cmd_gap_discover(1))
        ble.check_activity(ser, 1)
        
    # get handle to check_activity() and time.sleep() functions for performance
    check_ser = ble.check_activity
    sleep_CPU = time.sleep
        
    while not returned_to_idle:
        # check for all incoming data (no timeout, non-blocking)
        check_ser(ser)
        
        # don't burden the CPU
        sleep_CPU(sleep_time)
            
def test_previous_connections():
    global ble, ser, previous_connection_handles, number_of_imus, remaining_connections, peripheral_list
    
    # test if we are still connected to these sensors
    for handle in previous_connection_handles:
        number_of_imus = remaining_connections = len(previous_connection_handles)
        ble.send_command(ser, ble.ble_cmd_connection_get_rssi(handle))
        ble.check_activity(ser, 1)
        
    # check that we are connected to all of the previous IMUs
    connections_retained = 0
    for bd_addr in previous_peripheral_list:
        if bd_addr in peripheral_list:
            connections_retained += 1
    if connections_retained != len(previous_peripheral_list):
        print("There was a problem connecting to one or more of the sensors. \nPlease turn all IMUs off and then on again and attempt to reconnect")
    else:
        print("Successfully retained all previous IMU connections")
        
# this function is run in each user input thread to enable the user to choose when they would like to start sampling/recording and stop sampling/recording
def user_input_background():
    global ble, ser, state, current_connection_handle, characteristics_handles, store_data, finished_sampling, configuring_sensors
    
    # check if the user presses ENTER
    while True:
        time.sleep(1) 
        if raw_input() == '':
            break
            
    # if we are recording/displaying the received samples then we should stop
    if (state == STATE_RECORDING_SAMPLES) or (state == STATE_DISPLAYING_PACKETS):
        finished_sampling = True
        state = STATE_FINISHED_SAMPLING
        
    # start recording the recieved sample packets in the .csv file
    elif state == STATE_SAMPLING:
        if store_data:
            print("IMU samples are being recorded. Press ENTER to stop recording:  ")
            state = STATE_RECORDING_SAMPLES
        else:
            state = STATE_DISPLAYING_PACKETS
        
        # start new thread to stop recording
        user_input_stop_thread = threading.Thread(target=user_input_background)
        user_input_stop_thread.daemon = True
        user_input_stop_thread.start()
    
    # if we are waiting to start sampling then we should start sampling when ENTER is pressed
    elif state == STATE_WAIT_START_SAMPLING:
        configuring_sensors = True
        state = STATE_CHECK_SENSOR_IDLE
        ble.send_command(ser, ble.ble_cmd_attclient_read_by_handle(connection_handles[current_connection_handle], characteristics_handles['state']))
        ble.check_activity(ser, 1)
    
def file_write_sample_data():
    global file_handle, sensor_data_queue
   
    while not sensor_data_queue.empty():
        
        # pull data off queue and put in correct format
        raw_data = sensor_data_queue.get()
        information = [raw_data[0], raw_data[1], raw_data[2][1], interpret_data(raw_data[2][2:])]

        # contruct the data string for the interpreted data
        # the sample data is interpreted differently for high and low frequency sampling
        if high_freq:
            # NOTE: need to adjust times for sequential sample in same packet
            # convert input data into the appropriate units and string structure
            data = "%d, %.6f, %d, %.6f, %.6f, %.6f\n%d, %.6f, %d, %.6f, %.6f, %.6f\n%d, %.6f, %d, %.6f, %.6f, %.6f\n" %(information[1] + 1, information[0], information[2], information[3][0]/2048.0, \
                  information[3][1]/2048.0, information[3][2]/2048.0, information[1] + 1, information[0], information[2], information[3][3]/2048.0, \
                  information[3][4]/2048.0, information[3][5]/2048.0, information[1] + 1, information[0], information[2], information[3][6]/2048.0, \
                  information[3][7]/2048.0, information[3][8]/2048.0)
        else:
            # convert input data into the appropriate units and string structure
            data = "%d, %.6f, %d, %.6f, %.6f, %.6f, %.6f, %.6f, %.6f, %.6f, %.6f, %.6f \n" %(information[1] + 1, information[0], information[2], information[3][0]/2048.0, \
                  information[3][1]/2048.0, information[3][2]/2048.0, information[3][3]*gyro_divisor, \
                  information[3][4]*gyro_divisor, information[3][5]*gyro_divisor, information[3][6]*0.3, \
                  information[3][7]*0.3, information[3][8]*0.3)
            
        file_handle.write(data)
        
    file_handle.close()
    
def turn_off_scan():
    global ble, ser
   
    # stop scanning if we are scanning already
    ble.send_command(ser, ble.ble_cmd_gap_end_procedure())
    ble.check_activity(ser, 1)
        
# gracefully exit without a big exception message if possible
def ctrl_c_handler(signal, frame):
    
    print 'Goodbye!'
    exit(0)

signal.signal(signal.SIGINT, ctrl_c_handler)

if __name__ == '__main__':
    main()

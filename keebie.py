#!/usr/bin/env python3
#
# Keebie by Robin Universe & Friends
#
from evdev import InputDevice, categorize, ecodes
import sys
import signal
import os
import json
import argparse
import time
import subprocess
from shutil import copytree, copyfile
from typing import Any, Dict, List, Optional
JSON = Dict[str, Any]

# Utilities

# Whether we should print debug information
printDebugs = False

# Hide some output not strictly needed for interactivity
quietMode = False

def dprint(*args, **kwargs) -> None:
    """Print debug info (or don't)"""
    if printDebugs:
        print(*args, **kwargs)

def qprint(*args, **kwargs) -> None:
    """Print less then necessary info (or don't)"""
    if not quietMode:
        print(*args, **kwargs)

# Global Constants

# A dict of script types and their interpreters with a trailing space
scriptTypes = {
    "script:": "bash ",
    "py:": "python ",
    "py2:": "python2 ",
    "py3:": "python3 ",
    "exec:": "",
}

# Global vars

# Path where default user configuration files and more and are installed
installDataDir = "/usr/share/keebie/"
# Path where user configuration files should be stored
dataDir = os.path.join(os.path.expanduser("~"), ".config", "keebie")
#dprint(dataDir)

# Cache the full path to the /layers directory
layerDir = os.path.join(dataDir, "layers")
# Cache the full path to the /devices directory
deviceDir = os.path.join(dataDir, "devices")
# Cache the full path to the /scripts directory
scriptDir = os.path.join(dataDir, "scripts")

# The path to store the PID of a running looping instance of keebie
pidPath = os.path.join(dataDir, "running.pid")

# Signal handling

# A bool to track if devices have beed grabbed
devicesAreGrabbed = False

# A bool to store if this process has writen to the PID file
savedPid = False
# A bool to store if the process has sent a pause signal to a running keebie loop
paused = False
# A bool to store if this process has been signaled to pause by another instance
havePaused = False

def signal_handler(signal, frame) -> None:
    end()

def end() -> None:
    """Properly close the device file and exit the script"""
    qprint() # Make sure there is a newline
    # If we need to clean up grabbed macroDevices
    if devicesAreGrabbed:
        # Ungrab all devices
        ungrabMacroDevices()
        # Cleanly close all devices
        closeDevices()

    # if we have told a running keebie loop to pause
    if havePaused:
        # Tell it to resume
        sendResume()

    # If we have writen to the PID file
    if savedPid:
        # Remove our PID files
        removePid()

    # Exit without error
    sys.exit(0)


# Key Ledger

class keyLedger():
    """A class for tracking which keys are pressed,
    as well how how long and how recently."""

    def __init__(self, name: str ="unnamed ledger"):
        self.name = name # Name of the ledger for debug prints

        # An int representing the state of the ledger;
        # 0, 1, 2, 3 : rising, falling, holding, stale
        self.state = 3
        # The timestamp of the last state change
        self.stateChangeStamp = time.time()
        # Are we peaking (adding new keys; rising or holding)
        self.peaking = False

        # Current history of recent key peaks
        self.history = ""
        self.histories = [] # List of flushed histories

        self.newKeys = [] # List of keys newly down
        self.lostKeys = [] # List of keys newly lost
        # List of keys being held down
        self.downKeys: List[str] = []

    def newKeysStr(self) -> str:
        """Return a str of concatenated new keys."""
        keysParsed = ""

        for keycode in self.newKeys: # For all new keys
            # Add them to the string along with a "+"
            keysParsed += keycode + "+"

        # Return the string we built with the trailing "+" stripped
        return keysParsed.rstrip("+")

    def lostKeysStr(self) -> str:
        """Return a str of concatenated lost keys."""
        keysParsed = ""

        for keycode in self.lostKeys: # For all lost keys
            # Add them to the string along with a "+"
            keysParsed += keycode + "+"

        # Return the string we built with the trailing "+" stripped
        return keysParsed.rstrip("+")

    def downKeysStr(self) -> str:
        """Return a str of concatenated down keys."""
        keysParsed = ""
        # For all down keys
        for keycode in self.downKeys:
            # Add them to the string along with a "+"
            keysParsed += keycode + "+"
        # Return the string we built with the trailing "+" stripped
        return keysParsed.rstrip("+")

    def stateChange(self, newState, timestamp = None) -> None:
        """Change the ledger state and record the timestamp."""
        if not self.state == newState: # If the newState is actually new
            self.state = newState # Change self.state

            if timestamp is None: # If no timestamp was specified
                timestamp = time.time() # Use the current time

            self.stateChangeStamp = timestamp # Record the timestamp
            # dprint(f"{self.name}) new state {newState} at {timestamp}")

    def stateDuration(self, timestamp = None) -> float:
        """Return a float of how long our current state has lasted."""
        if timestamp is None: # If no timestamp was specified
            timestamp = time.time() # Use the current time
        # Return the time since state change
        return timestamp - self.stateChangeStamp

    def addHistoryEntry(self, entry = None, held = None, timestamp = None) -> None:
        """Add an entry to our history."""
        if entry is None: # If no entry was specified
            # Use the currently down keys
            entry = self.downKeysStr()
        # If the key was held was not specified
        if held is None:
            # Set held True if the length of last state surpassed
            # holdThreshold setting
            held = self.stateDuration((timestamp)) > settings["holdThreshold"]
        # If held is True note that into the entry
        entry += "+HELD" * held
        # If the current history is not empty
        if self.history:
            # Add a "-" to our history to separate key peaks
            self.history += "-"
        # Add entry to our history
        self.history += entry

        dprint(f"{self.name}) added {entry} to history")
        # dprint(f"{self.name}) history is \"{self.history}\"")

    def flushHistory(self) -> None:
        """Flush our current history into our histories list."""
        dprint(f"{self.name}) flushing {self.history}")
        # Add our history to our histories
        self.histories += [self.history, ]
        # Clear our history
        self.history = ""

    def popHistory(self):
        """Pop the nest item out of our histories list and return it,
        returns a blank string if no history is available."""
        try:
            dprint(f"{self.name}) popping {self.histories[0]}")
            # Pop and return the first element of our histories list
            return self.histories.pop(0)

        except IndexError: # If no history is available
            return "" # Return an empty string

    def update(self, events=()) -> bool:
        """Update the ledger with an iteratable of key events
        (or Nones to update timers).
        Returns a bool if we flushed any histories this time.
        """
        # A bool to store if we flushed any histories this update
        flushedHistory = False

        for event in events: # For each passed event
            self.newKeys = [] # They are no longer new
            self.lostKeys = [] # What once was lost...

            # A float (or None) for the timestamp of the event,
            # will be passed to other methods
            timestamp: float|None = None
            # If the event is not None
            if event is not None:
                # Set timestamp to the event's timestamp
                timestamp = event.timestamp()
                # If the event is a related to a key, as opposed to a mouse
                # movement or something (At least I think thats what this does)
                if event.type == ecodes.EV_KEY:
                    # Convert our EV_KEY input event into a KeyEvent
                    event = categorize(event)
                    keycode = event.keycode # Store the event's keycode
                    keystate = event.keystate # Store the event's key state

                    # dprint(timestamp)

                    # If the keycode is a list of keycodes (it can happen)
                    if type(keycode) == list:
                        keycode = keycode[0] # Select the first one
                    # If the key is down
                    if keystate in (event.key_down, event.key_hold):
                        # If the key is not known to be down
                        if not keycode in self.downKeys:
                            # Add the key to our new keys
                            self.newKeys += [keycode, ]

                    elif keystate == event.key_up: # If the key was released
                        # If the key was in our down keys
                        if keycode in self.downKeys:
                            # Add the key to our lost keys
                            self.lostKeys += [keycode, ]

                        else: # If the key was not known to be down
                            # Print a warning
                            print(f"{self.name}) Untracked key {keycode} released.")

            # if we have new keys (rising edge)
            if not self.newKeys == []:
                # dprint()
                dprint(f"{self.name}) >{'>' * len(self.downKeys)} " \
                    f"rising with new keys {self.newKeysStr()}")
                # Add our new keys to our down keys
                self.downKeys += self.newKeys
                # Store that we are peaking
                self.peaking = True

                # If we are in combination mode
                if settings["multiKeyMode"] == "combination":
                    # Sort our down keys to negate the order they were added in
                    self.downKeys.sort()

                self.stateChange(0, timestamp) # Change to state 0

            elif not self.lostKeys == []: # If we lost keys (falling edge)
                # dprint()
                dprint(f"{self.name}) {'<' * len(self.downKeys)}" \
                    f" falling with lost keys {self.lostKeysStr()}")

                if self.peaking:  # If we were peaking
                    # Add current down keys (peak keys) to our history
                    self.addHistoryEntry(timestamp=timestamp)
                    self.peaking = False # We are no longer peaking

                # For each lost key
                for keycode in self.lostKeys:
                    # Remove it from our down keys
                    self.downKeys.remove(keycode)

                self.stateChange(1, timestamp) # Change to state 1

            # If no keys were added or lost, but we still have down keys (holding)
            elif not self.downKeys == []:
                # dprint(end = f"{self.name}) {'-' * len(self.downKeys)}" \
                #     f" holding with down keys {self.downKeysStr()}" \
                #     f" since {str(self.stateChangeStamp)[7:17]}" \
                #     f" for {str(self.stateDuration(timestamp))[0:10]}" \
                #     f" {'held' * (self.stateDuration((timestamp)) > settings['holdThreshold'])}\r")
                # Change to state 2
                self.stateChange(2, timestamp)

            # If no keys were added or lost but we don't have any down keys (stale)
            else:
                # dprint(end = f"{self.name}) stale since {str(self.stateChangeStamp)[7:17]}" \
                #     f" for {str(self.stateDuration(timestamp))[0:10]}\r")
                self.stateChange(3, timestamp) # Change to state 3
                # If the duration of this stale state has surpassed
                # flushTimeout setting
                if self.stateDuration(timestamp) > settings["flushTimeout"] and self.history:
                    # dprint()
                    self.flushHistory() # Flush our current history
                    flushedHistory = True # Store that we did so

        # Return whether we flushed any histories
        return flushedHistory



class macroDevice():
    """
    Macro device
    A class for managing devices.
    """

    @property
    def currentLayer(self) -> str:
        '''
        Name of the Layer this device is currently on
        '''
        return self._currentLayer

    @currentLayer.setter
    def currentLayer(self, val: str) -> None:
        '''
        Set the Name of the Layer this device is currently on
        '''
        self._currentLayer = val
        self._currentLayerJson: Optional[JSON] = None
        return

    @property
    def currentLayerJson(self) -> JSON:
        if self._currentLayerJson is None:
            self._currentLayerJson = readJson(self.currentLayer)
        return self._currentLayerJson

    def __init__(self, deviceJson) -> None:
        # Name of device for debugging
        self.name = deviceJson.split(".json")[0]
        # Cache the data held in the device json file
        jsonData = readJson(deviceJson, deviceDir)
        # Layer for the device the start on
        self.initialLayer = jsonData["initial_layer"]
        # Layer this device is currently on
        self.currentLayer = self.initialLayer
        # The input event file
        self.eventFile = "/dev/input/" + jsonData["event"]
        # Strings for udev matching
        self.udevTests = jsonData["udev_tests"]
        # A keyLedger to track input events on his devicet
        self.ledger = keyLedger(self.name)
        self.device: Optional[InputDevice] = None

    def addUdevRule(self, priority = 85) -> None:
        """Generate a udev rule for this device."""
        # Name of the file for the rule
        path = f"{priority}-keebie-{self.name}.rules"
        rule = ""

        for test in self.udevTests: # For all the udev tests
            rule += test + ", " # Add them together with commas
            dprint(rule)

        # Save the udev rule filepath for removeDevice()
        writeJson(self.name + ".json", {"udev_rule": path}, deviceDir)

        # Run the udev setup script with sudo
        subprocess.run(
            ["sudo", "sh", installDataDir + "/setup_tools/udevRule.sh",
             rule, path])

        # Force udev to parse the new rule for the device
        subprocess.run(
            ["sudo", "udevadm", "test", "/sys/class/input/event3"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def grabDevice(self) -> None:
        """Grab the device and set self.device to the grabbed device."""
        qprint("grabbing device " + self.name)
        # Set self.device to the device of self.eventFile
        self.device = InputDevice(self.eventFile)
        qprint("device:", self.name)
        try:
            self.device.grab()
        except (json.decoder.JSONDecodeError, OSError) as e:
            print("Error grabbing device:", e)

        # Set the leds based on the current layer
        self.setLeds()

    def ungrabDevice(self):
        """Ungrab the device."""
        qprint("ungrabbing device " + self.name)
        # Do the thing that got said twice
        self.device.ungrab()

    def close(self):
        """Try to close the device file gracefully."""
        qprint("closing device " + self.name)

        self.device.close() # Close the device

    def read(self, process=True) -> bool:
        """
        Read all queued events (if any), update the ledger,
        and process the keycodes (or don't).
        Returns a bool if we flushed any histories this time
        """

        # flushed any histories this time?
        flushedHistories = False
        try:
            # Update our ledger with any available events
            flushedHistories = self.ledger.update(self.device.read())

        except BlockingIOError:
            # no events are available
            # Update our ledger so things get flushed if need be
            flushedHistories = self.ledger.update((None, ))

        # If we are processing the ledger
        if process and flushedHistories:
            self.processLedger() # Process the newly updated ledger

        return flushedHistories # Return whether we flushed any histories

    def setLeds(self) -> None:
        """Set device leds based on the current layer."""
        # If the current layer specifies LEDs
        if "leds" in self.currentLayerJson:
            # Check if the device had LEDs
            if 17 in self.device.capabilities().keys():
                # Get a list of LEDs the device has
                leds = self.device.capabilities()[17]
                # Get a list of LEDs to turn on
                onLeds = self.currentLayerJson["leds"]
                dprint(f"device {self.name} setting leds {onLeds} on")
                for led in leds: # For all LEDs on the board
                    if led in onLeds: # If the LED is to be set on
                        self.device.set_led(led, 1) # Set it on
                    else:
                        self.device.set_led(led, 0) # Set it off

            else:
                dprint("Device has no LEDs")

        else:
            print(f"Layer {self.currentLayerJson} has no leds property, writing empty")
            # Write an empty list for LEDs into the current layer
            writeJson(self.currentLayer, {"leds": []})
            self._currentLayerJson = None
            # For all LEDs on the board
            for led in self.device.capabilities()[17]:
                self.device.set_led(led, 0) # Set it off

    def processLedger(self) -> None:
        """Process any flushed histories from our ledger."""
        keycode = self.ledger.popHistory()
        # As long as the history we have isn't blank
        while keycode:
            self.processKeycode(keycode)
            # And grab the next one (blank if none are available)
            keycode = self.ledger.popHistory()

    def processKeycode(self, keycode) -> None:
        """
        Parse a command in our current layer bound to the passed keycode
        (ledger history).
        """
        # Print debug info
        dprint(f"{self.name} is processing {keycode} in layer {self.currentLayer}")

        value = self.currentLayerJson.get(keycode, None)
        if value is None:
            # the keycode is NOT in our current layer's json
            return

        # Parse any varables that may appear in the command
        value = parseVars(value, self.currentLayerJson)
        if value.startswith("layer:"):
            # this is a layer switch command
            jfile = value.split(':')[-1] + ".json"
            jpath = os.path.join(layerDir, jfile)
            if not os.path.exists(jpath):
                createLayer(jfile)
                print("Created layer file:", jfile)
            self.currentLayer = jfile

            # Set LEDs based on the new current layer
            self.setLeds()
            return
        #
        # sanitize value according to
        # settings forceBackground and backgroundInversion
        #
        value = value.strip()
        if settings["forceBackground"] and not value.endswith("&"):
            value += " &"

        # If value is not set to run in the background and
        # our settings say to invert background mode
        if settings["backgroundInversion"] and not value.endswith("&"):
            value += " &" # Force running in the background

        # Else if value is set to run in the background and our settings
        # say to invert background mode
        elif value.endswith("&") and settings["backgroundInversion"]:
            # Remove all spaces and &s from the end of value, there
            # might be a better way but this is the best I've got
            value = value.rstrip(" &")
        #
        # Does the value start with the recognized script type?
        #
        for scriptType, prefix in scriptTypes.items():
            if value.startswith(scriptType):
                scriptPath = os.path.join(scriptDir, value.split(':')[-1])
                value = prefix + scriptPath
                break
        #
        # If this is not a script (i.e. it is a shell command)
        #
        else:
            # Notify the user of the command
            print(keycode + ": " + value)

        # Notify the user we are running a script
        print("Executing", value)
        os.system(value) # Execute value

    def clearLedger(self) -> None:
        """Clear this devices ledger."""
        try:
            # For all queued events
            for event in self.device.read():
                # Completely ignore them
                pass
        # If there arn't any queued events
        except BlockingIOError:
            pass # Ignore that too
        # Reset the ledger
        self.ledger = keyLedger(self.name)


# List of macroDevice instances
macroDeviceList: List[macroDevice] = []

# A dict of standard LED ids and their names
standardLeds = {
    0: "num lock",
    1: "caps lock",
    2: "scroll lock",
}

def setupMacroDevices() -> None:
    """Setup a macroDevice instance based on the contents of deviceDir."""
    global macroDeviceList # Globalize macroDeviceList
    deviceJsonList = [
        # Get list of json files in deviceDir
        deviceJson for deviceJson in os.listdir(deviceDir)
        if os.path.splitext(deviceJson)[1] == ".json"
    ]
    dprint(deviceJsonList) # Print debug info
    dprint([device.name for device in macroDeviceList])
    for device in macroDeviceList: # For all preexisting devices
        if device.name + ".json" not in deviceJsonList:
            # If a preexisting device is not in our list of devices
            dprint(f"Device {device.name} has been removed")
            # Delete it (It should already be closed)
            macroDeviceList.remove(device)

    dprint([device.name for device in macroDeviceList])

    # Set up an empty list for new devices
    newMacroDeviceList = []
    # For all json files in deviceDir
    for deviceJson in deviceJsonList:
        # For all the preexisting devices
        for device in macroDeviceList:
            if deviceJson == device.name + ".json":
                # If the new device is already known
                dprint(f"Device {device.name} already known")
                break

        else: # If the loop was never broken
            dprint("New device " + deviceJson)
            # Set up a macroDevice instance for all files and save them to
            # newMacroDeviceList
            newMacroDeviceList += [macroDevice(deviceJson), ]

    # Add the list ofnew devices to the list of preexisting ones
    macroDeviceList += newMacroDeviceList

def grabMacroDevices() -> None:
    """Grab all devices with macroDevices."""
    global devicesAreGrabbed # Globallize devicesAreGrabbed
    devicesAreGrabbed = True # And set it true
    qprint('macroDeviceList:', macroDeviceList)
    for device in macroDeviceList:
        device.grabDevice()

def ungrabMacroDevices() -> None:
    """Ungrab all devices with macroDevices."""
    global devicesAreGrabbed
    devicesAreGrabbed = False
    for device in macroDeviceList:
        device.ungrabDevice()

def closeDevices() -> None:
    """Close all the devices."""
    for device in macroDeviceList:
        device.close()

def mergeDeviceLedgers() -> keyLedger:
    """Merge the key ledgers of all macroDevices into one and return it."""
    # Create an empty key ledger
    returnLedger = keyLedger()
    for device in macroDeviceList:
        # Add the devices key lists to the return ledger
        returnLedger.newKeys += device.ledger.newKeys
        returnLedger.lostKeys += device.ledger.lostKeys
        returnLedger.downKeys += device.ledger.downKeys
        # Add the devices histories to the return ledger
        returnLedger.histories += device.ledger.histories
    # Return the ledger we built
    return returnLedger

def clearDeviceLedgers() -> None:
    """Clear all device ledgers."""
    for device in macroDeviceList:
        device.clearLedger()

def readDevices(process=True) -> bool:
    """
    Read and optionally process the events from all the devices.
    Returns a bool if we flushed any histories this time.
    """
    flushedHistories = False
    for device in macroDeviceList:
        if device.read(process):
            flushedHistories = True
    return flushedHistories

def popDeviceHistories():
    """Pop and return all histories of all devices as a list."""
    # A list for poped histories
    histories = []
    for device in macroDeviceList:
        keycode = device.ledger.popHistory()
        # As long as the history we have isn't blank
        while keycode:
            # Add it to the list
            histories += [keycode, ]
            # And grab the next one (blank if none are available)
            keycode = device.ledger.popHistory()

    # Return the histories we got
    return histories



# JSON

def readJson(filename: str, dir: str = layerDir) -> JSON:
    """Reads the JSON file contents in the directory dir)"""
    path = os.path.join(dir, filename)
    dprint('readJson', path)
    res = {}
    with open(path) as f:
        try:
            res = json.load(f)
        except json.decoder.JSONDecodeError as err:
            print(f'Syntax error in {path}: {err}')
    dprint('readJson', path, '=>', res)
    return res

def writeJson(filename: str, data: JSON, dir: str = layerDir) -> None:
    """Appends new data to a specified layer (or any json file named filename
    in the directory dir)"""
    fpath = os.path.join(dir, filename)
    try:
        # Open an existing file
        with open(fpath) as f:
            # And copy store its data
            prevData = json.load(f)
    # If the file doesn't exist
    except FileNotFoundError:
        prevData = {}

    prevData.update(data)
    with open(fpath, 'w+') as outfile:
        json.dump(prevData, outfile, indent=3)

def popDictRecursive(dct, keyList) -> None:
    """Given a dict and list of key names of dicts follow said list into the
    dicts recursivly and pop the finall result, it's hard to explain"""
    if len(keyList) == 1:
        dct.pop(keyList[0])
    elif len(keyList) > 1:
        popDictRecursive(dct[keyList[0]], keyList[1:])

def popJson(filename, key, dir = layerDir) -> None:
    """Removes the key key and it's value from a layer (or any json file named
    filename in the directory dir)"""
    fpath = os.path.join(dir, filename)
    with open(fpath) as f:
        prevData = json.load(f)

    if type(key) == str:
        prevData.pop(key)
    elif type(key) == list:
        popDictRecursive(prevData, key)

    with open(fpath, 'w+') as outfile:
        json.dump(prevData, outfile, indent=3)



# Layer file

def createLayer(filename: str) -> None:
    """Creates a new layer with a given filename"""
    # Copy the provided default layer file from installedDataDir to the
    # specified filename
    src = os.path.join(installDataDir, "/data/layers/default.json")
    dst = os.path.join(layerDir, filename)
    copyfile(src, dst)


# Settings file

# A dict of settings to be used across the script
settings = {
    "multiKeyMode": "combination",
    "forceBackground": False,
    "backgroundInversion": False,
	"loopDelay": 0.0167,
    "holdThreshold": 1,
    "flushTimeout": 0.5,
}

# A dict of lists of valid values for each setting (or if first element is type
# then list of acceptable types in descending priority)
settingsPossible = {
    "multiKeyMode": ["combination", "sequence"],
    "forceBackground": [True, False],
    "backgroundInversion": [True, False],
	"loopDelay": [type, float, int],
    "holdThreshold": [type, float, int],
    "flushTimeout": [type, float, int],
}

def getSettings() -> None:
    """Reads the json file specified on the third line of config and sets the
    values of settings based on it's contents"""
    # Notify the user we are getting settings and tell them the file we are
    # using to do so
    dprint(f"Loading settings from {dataDir}/settings.json")
    global settings
    # Get a dict of the keys and values in our settings file
    settingsFile = readJson("settings.json", dataDir)
    # For every setting we expect to be in our settings file
    for setting in settings.keys():
        # If first element is type
        if type == settingsPossible[setting][0]:
            # If the value in our settings file is valid
            if type(settingsFile[setting]) in settingsPossible[setting]:
                dprint(f"Found valid typed value: \"{type(settingsFile[setting])}\" for setting: \"{setting}\"")
                # Write it into our settings
                settings[setting] = settingsFile[setting]
            else :
                # Warn the user of invalid settings in the settings file
                print(f"Value: \"{settingsFile[setting]}\" for setting: \"{setting}\" is of invalid type, defaulting to {settings[setting]}")
        # If the value in our settings file is valid
        elif settingsFile[setting] in settingsPossible[setting]:
            dprint(f"Found valid value: \"{settingsFile[setting]}\" for setting: \"{setting}\"")
            # Write it into our settings
            settings[setting] = settingsFile[setting]
        else :
            # Warn the user of invalid settings in the settings file
            print(f"Value: \"{settingsFile[setting]}\" for setting: \"{setting}\" is invalid, defaulting to {settings[setting]}")
    # Debug info
    dprint(f"Settings are {settings}")

# Keypress processing

def parseVars(commandStr, layerJson) -> str:
    """
    Given a command from the layer json file replace vars with their values
    and return the string
    """
    # Vars we will need in the loop
    returnStr = "" # The string to be retuned
    escaped = False # If we previously encountered an escape char
    escapeChar = "\\" # What is out escape char
    varChars = ("%", "%") # What characters start and end a varable name
    inVar = False # If we are in a varable name
    varName = "" # What the varables name is so far

    # Iterate over the chars of the input
    for char in commandStr :
        if escaped:
            # If char is escaped add it unconditionally and reset escaped
            returnStr += char
            escaped = False
            continue

        if escaped == False and char == escapeChar :
            # If char is en unescaped escape char set escaped
            escaped = True
            continue

        if inVar == False and char == varChars[0] :
            # If we arn't in a varable and chars is the start of one set inVar
            inVar = True
            continue

        if inVar and char == varChars[1] :
            # If we are in a varable and char ends it parse the varables value,
            # add it to returnStr if valid, and reset inVar and varName
            try :
                returnStr += layerJson["vars"][varName]
            except KeyError :
                print(f"unknown var {varName} in command {commandStr}, skiping command")
                return ""

            inVar = False
            varName = ""
            continue

        # If we are in a varable name add char to varName
        if inVar:
            varName += char
            continue

        # If none of the above (because we use continue) add char to returnStr
        returnStr += char

    # All done, return the result
    return returnStr

def getHistory():
    """Return the first key history we get from any of our devices"""
    clearDeviceLedgers() # Clear all device ledgers

    # Read events until a history is flushed
    while not readDevices(False):
        # Sleep so we don't eat the poor little CPU
        time.sleep(settings["loopDelay"])

    # Store the first history
    return popDeviceHistories()[0]


# Shells

def getLayers():
    """Lists all the json files in /layers and their contents"""
    print("Available Layers: \n")
    layerFt = ".json"
    layerFi = {}
    # Get a list of paths to all files that match our file extension
    layers = [
        i for i in os.listdir(layerDir) if os.path.splitext(i)[1] == layerFt
    ]
    for f in layers:
        with open(os.path.join(layerDir, f)) as file_object:
            # Build a list of the files at those paths
            layerFi[f] = file_object.read()

    for i in layerFi:
        # And display thier contents to the user
        print(i + layerFi[i])
    end()

def detectKeyboard(path = "/dev/input/by-id/"):
    """Detect what file a keypress is coming from"""
    # Warn the user we need sudo
    print("Gaining sudo to watch root owned files, sudo may prompt you for a password")
    # Get sudo
    subprocess.run(["sudo", "echo",  "have sudo"])

    print("Please press a key on the desired input device...")
    # Small delay to avoid detecting the device you started the script with
    time.sleep(.5)
    dev = ""
    # Wait for this command to output the device name, loops every 1s
    while not dev:
        dev = subprocess.check_output("sudo inotifywatch " + path +"/* -t 1 2>&1 | grep " + path + " | awk 'NF{ print $NF }'", shell=True ).decode('utf-8').strip()
    return dev

def addKey(layer = "default.json", key = None, command = None,
           keycodeTimeout = 1) -> None:
    """Shell for adding new macros"""
    if key is None and command is None:
        relaunch = True
    else:
        relaunch = False

    if command is None:
        # Get the command the user wishs to bind
        command = input("Enter the command you would like to attribute to a key on your second keyboard \n")
        # If the user entered a layer switch command
        if command.startswith("layer:"):
            # Check if the layer json file exsits
            if not os.path.exists(command.split(':')[-1] + ".json"):
                # If not create it
                createLayer(command.split(':')[-1] + ".json")
                # And notify the user
                print("Created layer file: " + command.split(':')[-1] + ".json")

                print("standard LEDs:")
                for led in standardLeds.items(): # For all LEDs on most boards
                    print(f"-{led[0]}: {led[1]}") # List it

                # Prompt the user for a list of LED numbers
                onLeds = input("Please choose what LEDs should be enable on this layer (comma and/or space separated list)")
                onLeds = onLeds.replace(",", " ").split() # Split the input list

                onLedsInt = []
                for led in onLeds: # For all strs in the split list
                    # Cast the str to int and add it to a list
                    onLedsInt.append(int(led))
                # Write the input list to the layer file
                writeJson(command.split(':')[-1] + ".json", {"leds": onLedsInt})

    if key is None:
        print(f"Please the execute keystrokes you would like to assign the command to and wait for the next prompt.")
        key = getHistory()

    # Ask the user if we (and they) got the command and binding right
    inp = input(f"Assign {command} to [{key}]? [Y/n] ")
    if inp == 'Y' or inp == '': # If we did
        newMacro = {}
        newMacro[key] = command
        # Write the binding into our layer json file
        writeJson(layer, newMacro)
        print(newMacro)

    else:
        # Confirm we have cancelled the binding
        print("Addition cancelled.")

    if relaunch:
        # Offer the user to add another binding
        rep = input("Would you like to add another Macro? [Y/n] ")
        if rep == 'Y' or rep == '': # If they say yes
            addKey(layer) # Restart the shell
        end()

def editSettings() -> None:
    """Shell for editing settings"""

    # Get a dict of the keys and values in our settings file
    settingsFile = readJson("settings.json", dataDir)
    # Create a list for key-value pairs of settings
    settingsList = []
    # For every key-value pair in our settings dict
    for setting in settings.items():
        # Add the pair to our list of seting pairs
        settingsList += [setting, ]
    # Ask the user to choose which setting they wish to edit
    print("Choose what value you would like to edit.")
    # Print an entry for every setting, as well as a number associated with
    for settingIndex in range(0, len(settingsList)):
        # Print an entry for every setting, as well as a number associated with
        # it and it's current value
        print(f"-{settingIndex + 1}: {settingsList[settingIndex][0]}   [{settingsList[settingIndex][1]}]")

    # Take the users input as to which setting they wish to edit
    selection = input("Please make you selection: ")

    try:
        # Convert the users input from str to int
        intSelection = int(selection)

    except ValueError: # If the conversion to int fails
        print("Exiting...") # Tell the user we are exiting
        end() # And do so

    if intSelection in range(1, len(settingsList) + 1):
        # If the users input corresponds to a listed setting
        # Store the selected setting's name
        settingSelected = settingsList[int(selection) - 1][0]
        # Tell the user we are thier selection
        print(f"Editing item \"{settingSelected}\"")

    else: # If the users input does not correspond to a listed setting
        print("Input out of range, exiting...") # Tell the user we are exiting
        end() # And do so

    if type == settingsPossible[settingSelected][0]:
        # If first element of settingsPossible is type
        print(f"Enter a value {settingSelected} that is of one of these types.")
        # For the index number of every valid type of the users selected setting
        for valueIndex in range(1, len(settingsPossible[settingSelected])):
            # Print an entry for every valid type
            print("- " + settingsPossible[settingSelected][valueIndex].__name__)
        # Prompt the user for input
        selection = input("Please enter a value: ")
        if not selection: # If none is provided
            print("Exiting...")
            end() # Exit
        # For all the valid types
        for typePossible in settingsPossible[settingSelected]:
            dprint(typePossible)
            if typePossible == type: # If it is type
                continue
            try:
                # Cast the users input to the type
                selection = typePossible(selection)
                break
            except ValueError: # If casting fails
                pass

        # If we have successfully casted to a valid type
        if type(selection) in settingsPossible[settingSelected]:
            # Write the setting into the settings file
            writeJson("settings.json", {settingSelected: selection}, dataDir)
            print(f"Set \"{settingSelected}\" to \"{selection}\"")
        else:
            # Complain about the bad input
            print("Input can't be casted to a supported type, exiting...")
            end() # And exit

    else:
        # Ask the user to choose which value they want to assign to their
        # selected setting
        print(f"Choose one of {settingSelected}\'s possible values.")
        # For the index number of every valid value of the users selected setting
        for valueIndex in range(0, len(settingsPossible[settingSelected])):
            # Print an entry for every valid value, as well as a number
            # associate, with no newline
            print(f"-{valueIndex + 1}: {settingsPossible[settingSelected][valueIndex]}", end = "")
            # If a value is the current value of the selected setting
            if settingsPossible[settingSelected][valueIndex] == settings[settingSelected]:
                # Tell the user and add a newline
                print("   [current]")
            else:
                # Add a newline
                print()
        # Take the users input as to which value they want to assign to their
        # selected setting
        selection = input("Please make you selection: ")

        try:
            # Convert the users input from str to int
            intSelection = int(selection)
            # If the users input corresponds to a listed value
            if intSelection in range(1, len(settingsPossible[settingSelected]) + 1):
                # Store the selected value
                valueSelected = settingsPossible[settingSelected][int(selection) - 1]
                # Write it into our settings json file
                writeJson("settings.json", {settingSelected: valueSelected}, dataDir)
                # And tell the user we have done so
                print(f"Set \"{settingSelected}\" to \"{valueSelected}\"")

            else: # If the users input does not correspond to a listed value
                # Tell the user we are exiting
                print("Input out of range, exiting...")
                end() # And do so

        except ValueError: # If the conversion to int fails
            print("Exiting...") # Tell the user we are exiting
            end() # And do so
    # Refresh the settings in our settings dict with the newly changed setting
    getSettings()
    # Offer the user to edit another setting
    rep = input("Would you like to change another setting? [Y/n] ")
    if rep == 'Y' or rep == '': # If they say yes
        editSettings() # Restart the shell
    else:
        end()

def editLayer(layer = "default.json"):
    """Shell for editing a layer file (default by default)"""
    # Get a dict of keybindings in the layer file
    LayerDict = readJson(layer, layerDir)

    keybindingsList = [] # Create a list for key-value pairs of keybindings
    # For every key-value pair in our layers dict
    for keybinding in LayerDict.items():
        # Add the pair to our list of keybinding pairs
        keybindingsList += [keybinding, ]
    # Ask the user to choose which keybinding they wish to edit
    print("Choose what binding you would like to edit.")
    # For the index number of every binding pair in our list of binding pairs
    for bindingIndex in range(0, len(keybindingsList)):
        if keybindingsList[bindingIndex][0] == "leds":
            print(f"-{bindingIndex + 1}: Edit LEDs")
        elif keybindingsList[bindingIndex][0] == "vars":
            print(f"-{bindingIndex + 1}: Edit layer varables")
        else:
            # Print an entry for every binding, as well as a number associated
            # with it and it's current value
            print(f"-{bindingIndex + 1}: {keybindingsList[bindingIndex][0]}   [{keybindingsList[bindingIndex][1]}]")
    # Take the users input as to which binding they wish to edit
    selection = input("Please make you selection: ")

    try:
        intSelection = int(selection) # Comvert the users input from str to int
        # If the users input corresponds to a listed binding
        if intSelection in range(1, len(keybindingsList) + 1):
            # Store the selected bindings's key
            bindingSelected = keybindingsList[int(selection) - 1][0]
            # Tell the user we are editing their selection
            print(f"Editing item \"{bindingSelected}\"")

        # If the users input does not correspond to a listed binding
        else:
            # Tell the user we are exiting
            print("Input out of range, exiting...")
            end() # And do so

    except ValueError: # If the conversion to int fails
        print("Exiting...") # Tell the user we are exiting
        end() # And do so

    if bindingSelected == "leds":
        print("standard LEDs:")
        for led in standardLeds.items(): # For all LEDs on most boards
            print(f"-{led[0]}: {led[1]}") # List it

        # Prompt the user for a list of LED numbers
        onLeds = input("Please choose what LEDs should be enable on this layer (comma and/or space separated list)")
        onLeds = onLeds.replace(",", " ").split() # Split the input list

        onLedsInt = []
        for led in onLeds: # For all strs in the split list
            # Cast the str to int and add it to a list
            onLedsInt.append(int(led))
        # Write the input list to the layer file
        writeJson(layer, {"leds": onLedsInt})

    elif bindingSelected == "vars":
        # Get a dict of layer vars in the layer file
        varsDict = readJson(layer, layerDir)["vars"]

        varsList = [] # Create a list for key-value pairs of layer vars
        # For every key-value pair in our layer vars dict
        for var in varsDict.items():
            varsList += [var, ] # Add the pair to our list of layer var pairs

        # Ask the user to choose which var they wish to edit
        print("Choose what varable you would like to edit.")
        # For the index number of every var pair in our list of var pairs
        for varIndex in range(0, len(varsList)):
            print(f"-{varIndex + 1}: {varsList[varIndex][0]}   [{varsList[varIndex][1]}]")

        # Take the users input as to which var they wish to edit
        selection = input("Please make you selection: ")

        try:
            # Comvert the users input from str to int
            intSelection = int(selection)
            # If the users input corresponds to a listed var
            if intSelection in range(1, len(varsList) + 1):
                # Store the selected var's key
                varSelected = varsList[int(selection) - 1][0]
                # Tell the user we are editing their selection
                print(f"Editing item \"{varSelected}\"")

            # If the users input does not correspond to a listed var
            else:
                # Tell the user we are exiting
                print("Input out of range, exiting...")
                end() # And do so

        except ValueError: # If the conversion to int fails
            print("Exiting...") # Tell the user we are exiting
            end() # And do so

        # Ask the user to choose what they want to do with their selected var
        print(f"Choose am action to take on {varSelected}.")
        # Prompt the user with a few possible actions
        print("-1: Delete varable.")
        print("-2: Edit varable name.")
        print("-3: Edit varable value.")
        print("-4: Cancel.")

        # Take the users input as to what they want to do with their selected var
        selection = input("Please make you selection: ")
        try:
            # Convert the users input from str to int
            intSelection = int(selection)
            if intSelection == 1: # If the user selected delete
                popJson(layer, ["vars", varSelected]) # Remove the var
            elif intSelection == 2: # If the user selected edit name
                # Ask the user for a new name
                varName = input("Please input new name: ")
                # Add new name and value to varDict
                varsDict.update({varName: varsDict[varSelected]})
                # Set layer's vars to varDict
                writeJson(layer, {"vars": varsDict})
                # Note: if the user replaces the original name with the same
                # name this will delete the binding
                popJson(layer, ["vars", varSelected])
            elif intSelection == 3: # If the user selected edit value
                # Ask the user for a new value
                varVal = input("Please input new value: ")
                # Update name to new value in varDict
                varsDict.update({varSelected: varVal})
                # Set layer's vars to varDict
                writeJson(layer, {"vars": varsDict})
            elif intSelection == 4: # If the user selected cancel
                pass # Pass back to the previous level

            else: # If the users input does not correspond to a listed value
                # Tell the user we are exiting
                print("Input out of range, exiting...")
                end() # And do so

        except ValueError: # If the conversion to int fails
            print("Exiting...") # Tell the user we are exiting
            end() # And do so

    else:
        # Ask the user to choose what they want to do with their selected binding
        print(f"Choose am action to take on {bindingSelected}.")
        # Prompt the user with a few possible actions
        print("-1: Delete binding.")
        print("-2: Edit binding key.")
        print("-3: Edit binding command.")
        print("-4: Cancel.")
        # Take the users input as to what they want to do with their selected binding
        selection = input("Please make you selection: ")
        try:
            # Convert the users input from str to int
            intSelection = int(selection)
            if intSelection == 1: # If the user selected delete
                popJson(layer, bindingSelected) # Remove the binding

            elif intSelection == 2: # If the user selected edit key
                # Launch the key addition shell and preserve the command
                addKey(layer, command = LayerDict[bindingSelected])
                # Note: if the user replaces the original key with the same key
                # this will delete the binding
                popJson(layer, bindingSelected)

            elif intSelection == 3: # If the user selected edit command
                # Launch the key addition shell and preserve the key
                addKey(layer, key = bindingSelected)

            elif intSelection == 4: # If the user selected cancel
                pass # Pass back to the previous level

            else: # If the users input does not correspond to a listed value
                # Tell the user we are exiting
                print("Input out of range, exiting...")
                end() # And do so

        except ValueError: # If the conversion to int fails
            print("Exiting...") # Tell the user we are exiting
            end() # And do so

    # Offer the user to edit another binding
    rep = input("Would you like to edit another binding? [Y/n] ")
    # If they say yes
    if rep == 'Y' or rep == '':
        # Restart the shell
        editLayer(layer)
    else:
        end()

def newDevice(eventPath: str = "/dev/input/") -> None:
    """Add a new json file to devices/."""
    print("Setting up device")

    # Prompt the user for a layer filename
    initialLayer = input("Please provide a name for for this devices initial layer (non-existent layers will be created, default.json by default): ")

    # If the user did not provide a layer name
    if initialLayer.strip() == "":
        initialLayer = "default.json" # Default to default.json

    if not os.path.exists(os.path.join(layerDir, initialLayer)):
        # If the users chosen layer does not exist
        createLayer(initialLayer) # Create it

    eventFile = detectKeyboard(eventPath) # Prompt the user for a device
    # Get the devices filename from its filepath
    eventFile = os.path.basename(eventFile)

    # Ensure the stdin is empty
    input("\nA udev rule will be made next, sudo may prompt you for a password. Press enter to continue...")
    # Make an udev rule matching the device file
    selectedPropertiesList = [f"KERNEL==\"{eventFile}\""]
    # Construct the device data dict
    deviceJsonDict = {
        "initial_layer": initialLayer,
        "event": eventFile,
        "udev_tests": selectedPropertiesList,
    }
    # Write device data into a json file
    writeJson(eventFile + ".json", deviceJsonDict, deviceDir)
    # Create a mcro device and make a udev rule,
    # the user will be prompted for sudo
    macroDevice(eventFile + ".json").addUdevRule()
    end()

def removeDevice(name: str = None) -> None:
    """Removes a device file from deviceDir and udev rule based on passed name.
    If no name is passed prompt the user to choose one."""
    if name is None:
        # no name was provided
        print("Devices:")
        # Get a list of device files
        deviceList = os.listdir(deviceDir)
        # For all device files
        for deviceIndex in range(0, len(deviceList)):
            # Print thier names
            print(f"-{deviceIndex + 1}: {deviceList[deviceIndex]}")

        # Prompt the user for a selection
        selection = int(input("Please make your selection : "))
        # Set name based on the users selection
        name = deviceList[selection - 1]

    # Cache the path to the devices udev rule
    udevRule = readJson(name, deviceDir)["udev_rule"]

    # Warn the user we need sudo
    print("removing device file and udev rule, sudo may prompt you for a password.")
    # Remove the device file
    os.remove(os.path.join(deviceDir, name))
    # Remove the udev rule
    subprocess.run([
        "sudo",
        "rm",
        "-f",
        os.path.join("/etc/udev/rules.d", udevRule)])
    end()

# Setup

def firstUses() -> None:
    """Setup to be run when a user first runs keebie"""
    # Copy template configuration files to user
    src = os.path.join(installDataDir, "data")
    copytree(src, dataDir, dirs_exist_ok=True)
    # And inform the user
    print(f"Configuration files copied from {src} to {dataDir}")


# Inter-process communication

def savePid() -> None:
    """Save our PID into the PID file.
    Raise FileExistsError if the PID file already exists."""
    dprint("Saving PID to", pidPath)

    if os.path.exists(pidPath):
        dprint("PID already recorded")
        raise FileExistsError("PID already recorded")
    # Create and open the PID file
    with open(pidPath, "wt") as pidFile:
        # Write our PID into it
        pidFile.write(str(os.getpid()))
        # Record that we have saved our PID
        global savedPid
        savedPid = True

def removePid() -> None:
    """Remove the PID file if it exists."""
    dprint("Removing PID file " + pidPath)
    # If the PID file exists
    if os.path.exists(pidPath):
        # Remove it
        os.remove(pidPath)
        # And record it's removal
        global savedPid
        savedPid = False
    else:
        print("PID was never stored?")

def getPid() -> int:
    """Return the PID stored in the PID file.
    Raise FileNotFoundError if the file does not exist."""
    if os.path.exists(pidPath):
        with open(pidPath, "rt") as pidFile:
            # And return it's contents as an int
            return int(pidFile.read())
    dprint("PID file dosn't exist")
    raise FileNotFoundError("PID file dosn't exist")

def checkPid() -> None:
    """Try to get the PID and check if it is valid.
    Raise FileNotFoundError if the PID file does not exist.
    Raise ProcessLookupError and remove the PID file if no process has the PID.
    """
    # Try to get the PID in the PID file,
    # this will raise en exception if the file is missing
    pid = getPid()
    try:
        # Send signal 0 to the process,
        # this will raise OSError if the process doesn't exist
        os.kill(pid, 0)

    except OSError:
        dprint("PID invalid")
        removePid() # Remove the PID file since its wrong
        raise ProcessLookupError("PID invalid")

def sendStop() -> None:
    """If a valid PID is found in the PID file send SIGINT to the process."""
    try:
        dprint("Sending stop")
        # Check if the PID file point's to a valid process
        checkPid()
        os.kill(getPid(), signal.SIGINT) # Stop the process

    except (FileNotFoundError, ProcessLookupError):
        # If the PID file doesn't exist or the process isn't valid
        dprint("No process to stop")

def sendPause(waitSafeTime = None) -> None:
    """If a valid PID is found in the PID file send SIGUSR1 to the process."""
    try:
        dprint("Sending pause")
        checkPid() # Check if the PID file point's to a valid process
        global havePaused
        havePaused = True # Save that we have paused the process
        os.kill(getPid(), signal.SIGUSR1) # Pause the process
        if waitSafeTime is None:
            # Set how long we should wait
            waitSafeTime = settings["loopDelay"] * 3
        # Wait a bit to make sure the process paused itself
        time.sleep(waitSafeTime)

    except (FileNotFoundError, ProcessLookupError):
        # If the PID file doesn't exist or the process isn't valid
        dprint("No process to pause")

def sendResume():
    """If a valid PID is found in the PID file send SIGUSR2 to the process."""
    try:
        dprint("Sending resume")
        # Check if the PID file point's to a valid process
        checkPid()
        global havePaused
        havePaused = False # Save that we have resumed the process
        os.kill(getPid(), signal.SIGUSR2) # Resume the process

    except (FileNotFoundError, ProcessLookupError):
        # If the PID file doesn't exist or the process isn't
        dprint("No process to resume")

def pause(signal, frame):
    """Ungrab all macro devices."""
    print("Pausing...")
    global paused
    paused = True # Save that we have been paused)
    # Ungrab all devices so the pausing process can use them
    ungrabMacroDevices()
    # Close our macro devices
    closeDevices()

def resume(signal, frame):
    """Grab all macro devices and refresh our setting after being paused
    (or just if some changes were made we need to load)."""
    print("Resuming...")
    getSettings() # Refresh our settings
    # If we were paused prior
    global paused
    if paused:
        # Set our macro devices up again to detect changes
        setupMacroDevices()
        # Grab all our devices back
        grabMacroDevices()

    # Save that we are no longer paused
    paused = False


# Arguments
def build_parser():
    parser = argparse.ArgumentParser() # Set up command line arguments
    parser.add_argument(
        "--layers", "-l", help="Show saved layer files", action="store_true")
    parser.add_argument(
        "--detect", "-d", help="Detect keyboard device file", action="store_true")
    parser.add_argument(
        "--print-keys", "-k", help="Print a series of keystrokes", action="store_true")
    try:
        parser.add_argument(
            "--add", "-a",
            help="Adds new macros to the selected layer file (or default layer if unspecified)",
            nargs="?",
            default=False, const="default.json", metavar="layer",
            choices=[
                i for i in os.listdir(layerDir)
                if os.path.splitext(i)[1] == ".json"
            ])
    except FileNotFoundError :
        parser.add_argument(
            "--add", "-a",
            help="Adds new macros to the selected layer file (or default layer if unspecified)", nargs="?",
            default=False, const="default.json", metavar="layer")

    parser.add_argument(
        "--settings", "-s", help="Edits settings file", action="store_true")

    try:
        parser.add_argument(
            "--edit", "-e",
            help="Edits specified layer file (or default layer if unspecified)",
            nargs="?", default=False, const="default.json", metavar="layer",
            choices=[
                i for i in os.listdir(layerDir)
                if os.path.splitext(i)[1] == ".json"
            ])
    except FileNotFoundError :
        parser.add_argument(
            "--edit", "-e",
            help="Edits specified layer file (or default layer if unspecified)",
            nargs="?", default=False, const="default.json", metavar="layer")

    parser.add_argument(
        "--new", "-n", help="Add a new device file", action="store_true")

    try:
        parser.add_argument(
            "--remove", "-r",
            help="Remove specified device, if no device is specified you will be prompted",
            nargs="?", default=False, const=True, metavar="device",
            choices=[
                i for i in os.listdir(deviceDir)
                if os.path.splitext(i)[1] == ".json"
            ])
    except FileNotFoundError :
        parser.add_argument(
            "--remove", "-r",
            help="Remove specified device, if no device is specified you will be prompted",
            nargs="?", default=False, const=True, metavar="device")

    parser.add_argument(
        "--pause", "-P",
        help="Pause a running keebie instance that is processing macros",
        action="store_true")

    parser.add_argument(
        "--resume", "-R",
        help="Resume a keebie instance paused by --pause", action="store_true")

    parser.add_argument(
        "--stop", "-S",
        help="Stop a running keebie instance that is processing macros",
        action="store_true")

    parser.add_argument(
        "--install", "-I",
        help="Install default files to your ~/.config/keebie directory",
        action="store_true")

    parser.add_argument(
        "--verbose", "-v",
        help="Print extra debugging information",
        action="store_true")

    parser.add_argument(
        "--quiet", "-q", help="Print less", action="store_true")
    return parser

def main():
    parser = build_parser()
    args = parser.parse_args()

    global printDebugs
    printDebugs = args.verbose
    global quietMode
    quietMode = args.quiet or args.print_keys

    if not args.print_keys:
        print("Welcome to Keebie")

    signal.signal(signal.SIGINT, signal_handler)

    if not os.path.exists(dataDir):
        # If the user we are running as does not have user configuration files
        # Inform the user
        print("You are running keebie without user configuration files installed")
        # Run first time user setup
        firstUses()

    # Setup all devices
    setupMacroDevices()
    # Get settings from the json file in config
    getSettings()

    if args.layers: # If the user passed --layers
        # Show the user all layer json files and their contents
        getLayers()

    elif args.print_keys:
        # Ask a running keebie loop (if one exists) to pause
        # so we can use the devices
        sendPause()
        grabMacroDevices()
        # Print the first key history we get from any of our devices
        print(getHistory())
        end()

    elif args.add: # If the user passed --add
        # Ask a running keebie loop (if one exists) to pause
        # so we can use the devices
        sendPause()

        grabMacroDevices()
        addKey(args.add) # Launch the key addition shell

    elif args.settings: # If the user passed --settings
        # Ask a running keebie loop (if one exists) to pause so it will reload
        # its settings when we're done
        sendPause()
        # Launch the setting editing shell
        editSettings()

    elif args.detect: # If the user passed --detect
        # Launch the keyboard detection function
        print(detectKeyboard("/dev/input/"))

    elif args.edit: # If the user passed --edit
         # Ask a running keebie loop (if one exists) to pause so we can use
         # the devices
        sendPause()
        grabMacroDevices()
        editLayer(args.edit) # Launch the layer editing shell

    elif args.new: # If the user passed --new
        # Ask a running keebie loop (if one exists) to pause so it will detect
        # the new device when we're done
        sendPause()
        # Launch the device addition shell
        newDevice()

    elif args.remove: # If the user passed --remove
         # Ask a running keebie loop (if one exists) to pause so it will detect
         # the removed device when we're done
        sendPause()
        # Launch the device removal shell
        removeDevice(args.remove)

    elif args.pause: # If the user passed --pause
        sendPause(0) # Ask a running keebie loop (if one exists) to pause
        havePaused = False

    elif args.resume: # If the user passed --resume
        sendResume() # Ask a running keebie loop (if one exists) to resume

    # If the user passed --stop
    elif args.stop:
        # Ask a running keebie loop (if one exists) to run end()
        sendStop()

    # If the user passed --install
    elif args.install:
        # Perform first time setup
        firstUses()

    # If the user passed nothing...
    else:
        try:
            # Try to save our PID to the PID file
            savePid()

        except FileExistsError:
            # the PID file already exists
            try:
                # Check if it is valid, this will raise an error if it isn't
                checkPid()
                print("Another instance of keebie is already processing macros, exiting...")
                end()

            # If the PID file points to an invalid PID...
            except ProcessLookupError:
                # Save our PID to the PID file
                # (which checkPid() will have removed)
                savePid()

        # Bind SIGUSR1 to pause()
        signal.signal(signal.SIGUSR1, pause)
        # Bind SIGUSR2 to remove()
        signal.signal(signal.SIGUSR2, resume)

        time.sleep(.5)
        grabMacroDevices() # Grab all the devices

        while True : # Enter an infinite loop
            if not paused:
                # Read all devices and process the keycodes
                readDevices()

            # Sleep so we don't eat the poor little CPU
            time.sleep(settings["loopDelay"])

if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
ZeTV (Sleeptest 2.2) — well-plate recording, stimulus, control, and per-well motion scoring.

Behavior: Records a plate assay, pulses an Arduino from a stimulus schedule featuring
   multicolored LEDs / solenoids, and optionally writes a motion index for each 
   of the 96 wells. Short assays run at 30 fps; Extended assays run at 2 fps and
   key the Arduino off the clock minute.

   Picamera2 (monochrome IMX296) records H264, mp4Box remuxes it at that frame
   rate, and OpenCV warps the plate before frame-subtraction. Calibration can
   lock the shutter speed, or the operator can type the four corners.
Exception: None at import. Each method reports its own failures.
Return: None. Runs until the window is closed.
Parameter: None. Start with `python3 ezmice.py`.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk
import threading
import datetime
from picamera2 import Picamera2, Preview

import time

import serial
from subprocess import call
import os
import cv2
import numpy as np
import csv



class EZMiceApp:
    """
    B: Operator window for one chamber's recording, stimulus, and analysis.
       Holds the form fields, Extended/Manual flags, Pi camera settings
       (shutter, frame rate), the Arduino thread, and the plate geometry.
       Only one Picamera2 exists at a time; self.busy is True while
       recording or calibrating owns it. Button callbacks start work; serial
       output, recording, and calibration run on daemon threads.
    E: None from construction. See each method for the errors it reports.
    R: None. Keep the instance alive for as long as master.mainloop() runs.
    P: master — the Tk root this UI is gridded onto.
    """

    def __init__(self, master):
        """
        B: Stores blank form fields, default plate geometry, and camera
           settings, then builds the window. Extended defaults off, so
           frame_rate starts at 30 and is rewritten from the checkbox when
           a recording starts. Plate corners default to a rectangle inside the
           old 1296x972 frame; the camera now runs at 1456x1088, so
           run Capture & Calibrate Plate (or type the corners) before the
           first recording. The warp target for live image processing
           is 600x400 with margins accounting for the shape of the plate.
           Manual corners default to TL (150, 100), TR (1150, 100),
           BL (150, 800), BR (1150, 800). The exposure (shutter speed) field
           is replaced when Capture & Calibrate samples the camera.
        E: None.
        R: None.
        P: master — Tk root window.
        """
        self.master = master
        self.master.title('EZMice v0.4.1')

        # clam renders ttk entries and buttons consistently on the Pi desktop.
        self.style = ttk.Style()
        self.style.theme_use('clam')

        # Operator form. Read at record time; nothing is validated until then.
        self.record_duration = tk.StringVar()
        self.chamber_number = tk.StringVar()
        self.animal_tag = tk.StringVar()
        self.save_path = tk.StringVar()
        self.recording = False
        self.calibrating = False
        # 0 = short assay at 30 fps, 1 = extended assay at 2 fps.
        self.extended_assay = tk.IntVar() # 0 "short" or 1 "extended"
        # Stimulus schedule text ("time:chars,...") and the serial tick length.
        self.stim_set = tk.StringVar()
        self.scheduled_start = tk.StringVar()
        self.shutdown = 'c'
        self.signal_tracker = set()
        self.signal_input = tk.StringVar()
        self.signal_list = ""
        self.signal_delay = 2

        # Source-image corners (TL, TR, BR, BL) and the rectangle they warp into.
        self.plate_corners = np.float32([[100, 100], [1196, 100], [1196, 872], [100, 872]])
        self.target_width = 600
        self.target_height = 400

        # Video processing toggle:
        self.processing_on = False

        # Manual corner entries. Hidden until the Manual checkbox is on.
        self.manual_cal = tk.IntVar()
        self.coord_entries = []
        self.tl_x = tk.IntVar(value=150)
        self.tl_y = tk.IntVar(value=100)
        self.tr_x = tk.IntVar(value=1150)
        self.tr_y = tk.IntVar(value=100)
        self.bl_x = tk.IntVar(value=150)
        self.bl_y = tk.IntVar(value=800)
        self.br_x = tk.IntVar(value=1150)
        self.br_y = tk.IntVar(value=800)
        # Pixel margin on the warped canvas. Scoring and the calibration grid
        # both step by (target - 2*margin) / wells.
        self.pmarg_x = 10
        self.pmarg_y = 0

        # Placeholder until record_with_preview reads the Extended checkbox.
        self.frame_rate = 2 if self.extended_assay.get() else 30
        # Microseconds. Calibration overwrites this from the camera's auto shutter.
        self.shutter = 100000

        self.create_widgets()

    def create_widgets(self):
        """
        B: Lays out the form, the Extended and Manual checkboxes, the action
           buttons, and the status line. Column 0 is labels, column 1 is
           entries, column 2 is Extended, Select Folder, and the capture
           buttons. The eight manual-corner entries are created but not
           gridded until toggle_coords. image_label is left empty; capture
           paths use a popup.
        E: None.
        R: None. Widgets that later methods update are stored on self.
        P: None. Uses self.master and the variables created in __init__.
        """
        ttk.Label(self.master, text='Record Duration (s):').grid(row=0, column=0, sticky='w', padx=5, pady=2)
        ttk.Label(self.master, text='Chamber Number:').grid(row=1, column=0, sticky='w', padx=5, pady=2)
        ttk.Label(self.master, text='Animal Tag:').grid(row=2, column=0, sticky='w', padx=5, pady=2)
        ttk.Label(self.master, text='Folder Path:').grid(row=3, column=0, sticky='w', padx=5, pady=2)
        ttk.Label(self.master, text='Stimulus Set:').grid(row=4, column=0, sticky='w', padx=5, pady=2)
        ttk.Label(self.master, text='Signal Input:').grid(row=5, column=0, sticky='w', padx=5, pady=2)
        ttk.Label(self.master, text='Scheduled Start Time (HH.MM):').grid(row=6, column=0, sticky='w', padx=5, pady=2)

        ttk.Checkbutton(self.master, text="Extended", variable=self.extended_assay).grid(row=0,column=2,padx=5,pady=2)
        self.duration_entry = ttk.Entry(self.master, textvariable=self.record_duration)
        self.duration_entry.grid(row=0, column=1, padx=5, pady=2)
        ttk.Entry(self.master, textvariable=self.chamber_number).grid(row=1, column=1, padx=5, pady=2)
        ttk.Entry(self.master, textvariable=self.animal_tag).grid(row=2, column=1, padx=5, pady=2)

        self.folder_entry = ttk.Entry(self.master, textvariable=self.save_path, state='readonly')
        self.folder_entry.grid(row=3, column=1, padx=5, pady=2)
        ttk.Button(self.master, text='Select Folder', command=self.select_folder).grid(row=3, column=2, padx=5, pady=2)

        # Free text, parsed by build_stim_dic. Wide entry so a long schedule fits.
        self.stim_set_entry = ttk.Entry(self.master, textvariable=self.stim_set)
        self.stim_set_entry.grid(row=4, column=1, columnspan=2, sticky='ew', padx=5, pady=2)

        # Signal list entry for the Arduino. Free text, parsed by send_serial.
        self.signal_input = ttk.Entry(self.master, textvariable=self.signal_input)
        self.signal_input.grid(row=5, column=1, sticky='ew', padx=5, pady=2)

        # Seconds between serial time checks. Empty means 0.05 in send_serial.
        self.start_entry = ttk.Entry(self.master, textvariable=self.scheduled_start)
        self.start_entry.grid(row=6, column=1, sticky='w', padx=5, pady=2)

        self.record_button = ttk.Button(self.master, text='Record & Analyze', command=self.toggle_record)
        self.record_button.grid(row=7, column=0, sticky='ew', padx=5, pady=5)

        ttk.Button(self.master, text='Capture Image', command=self.capture_and_display).grid(row=7, column=2, sticky='ew', padx=5, pady=5)
        self.calibrate_button = ttk.Button(self.master, text='Capture & Calibrate Plate', command=self.toggle_calibrate)
        self.calibrate_button.grid(row=8, column=2, sticky='ew', padx=5, pady=5)

        self.info_label = ttk.Label(self.master, text='')
        self.info_label.grid(row=8, column=0, columnspan=2, padx=5, pady=5)

        self.image_label = ttk.Label(self.master)
        self.image_label.grid(row=9, column=0, columnspan=3, sticky='nsew', padx=5, pady=5)

        # Manual checkbox reveals the eight corner entries via toggle_coords.
        ttk.Checkbutton(self.master, text="Manual", variable=self.manual_cal, command=self.toggle_coords).grid(row=7,column=3,padx=5,pady=2)
        self.coord_entries.append(ttk.Entry(self.master, textvariable=self.tl_x))
        self.coord_entries.append(ttk.Entry(self.master, textvariable=self.tl_y))
        self.coord_entries.append(ttk.Entry(self.master, textvariable=self.tr_x))
        self.coord_entries.append(ttk.Entry(self.master, textvariable=self.tr_y))
        self.coord_entries.append(ttk.Entry(self.master, textvariable=self.bl_x))
        self.coord_entries.append(ttk.Entry(self.master, textvariable=self.bl_y))
        self.coord_entries.append(ttk.Entry(self.master, textvariable=self.br_x))
        self.coord_entries.append(ttk.Entry(self.master, textvariable=self.br_y))
    
    def toggle_coords(self):
        """
        B: Shows or hides the eight manual corner entries. When Manual is on,
           they are placed in two rows of four. When it is off, they are
           removed from the grid and the previous automatic corners remain.
        E: None.
        R: None.
        P: None. Reads self.manual_cal and self.coord_entries.
        """
        for i, entry in enumerate(self.coord_entries):
            if (self.manual_cal.get()):
                entry.grid(row=int(i/4 - 1 + 10), column=int(i%4), padx=5, pady=2)
            else:
                entry.grid_remove()

    def apply_warp(self, img):
        """
        B: Stretches the calibrated plate so it fills the target rectangle.
           Maps self.plate_corners, ordered TL-TR-BR-BL, onto (0, 0),
           (target_width, 0), (target_width, target_height), (0, target_height).
           Pixels outside the quadrilateral are dropped.
        E: cv2.error if plate_corners are not four finite, non-collinear points.
        R: Warped image with the target size and the same number of channels as img.
        P: img — BGR or single-channel frame that contains plate_corners.
        """
        dst_pts = np.float32([
            [0, 0],
            [self.target_width, 0],
            [self.target_width, self.target_height],
            [0, self.target_height]
        ])
        matrix = cv2.getPerspectiveTransform(self.plate_corners, dst_pts)
        return cv2.warpPerspective(img, matrix, (self.target_width, self.target_height))

    def build_stim_dic(self):
        """
        B: Turns the Stimulus Set entry field into {time_key: command_characters}.
           An empty field selects a built-in schedule. By default, extended mode uses a schedule
           that runs by clock hour and minute. Short mode uses elapsed seconds: e.g.
           {1: "w", 2: "wr", 3: "rg", 4: "gv", 5: "vs", 6: "sh", 7: "h"}.
           A non-empty field is comma-separated "time:chars" items in either
           mode. Characters for the same time are concatenated; each is later
           written as a ASCII command to be processed by the arduino
        E: A missing colon or a non-numeric time is caught, shown in an error
           dialog (posted to the Tk thread), and not re-raised. The dict
           parsed before that item is returned.
        R: Dict mapping a time key to the serial characters due then.
        P: None. Reads self.stim_set and self.extended_assay.
        """
        stim_dic = {}
        try:
            if self.stim_set.get() != "":
                stim_times = self.stim_set.get().split(',')
                for stim_time in stim_times:
                    key_time = float(stim_time.split(':')[0])
                    value_assay = stim_time.split(':')[1]
                    if not (key_time in stim_dic):
                        stim_dic[key_time] = ""
                    stim_dic[key_time] += value_assay
            else:
                hour = datetime.datetime.now().hour + datetime.datetime.now().minute / 100.0
                stim_dic = (
                    {hour:"w", 23: "w", 9:"w", 23:"w"}
                    if self.extended_assay.get()
                    else {1: "w", 2: "wr", 3: "rg", 4: "gv", 5: "vs", 6: "sh", 7: "h"}
                )

        except Exception as e:
            self.master.after(0, lambda m=str(e): messagebox.showerror("Error", f"Stimulus set input error: {m}"))
        return stim_dic

    def switch_signal(self, arduino, duration, last_time):
           """
           B: Switches the specified signals for a given duration. The first and last
              characters in signal_list are sent as serial commands to the Arduino. At
              any given time, the tail of the signal_list is the item currently on and
              the head of the signal_list is the item to be turned on next. The signal_list
              rotates like a cyclic queue, iterating through all signals present in the list.
           E: None.
           R: None.
           P: None.
           """
           if time.perf_counter() - last_time >= duration:
               last_time = time.perf_counter()
               #signal_list.append(signal_list.pop(0))
               #Last list item is the signal currently "on",
               #first list item is next one to be turned "on"
               arduino.write(self.signal_list[0].encode('ascii'))
               self.signal_tracker.add(self.signal_list[0])
               arduino.write(self.signal_list[len(self.signal_list) - 1].encode('ascii'))
               self.signal_tracker.remove(self.signal_list[len(self.signal_list) - 1])
               self.signal_list = self.signal_list[1:] + self.signal_list[0]
           return last_time

    def send_serial(self):
        """
        B: Writes stimulus characters to the Arduino while a recording runs.
           Opens /dev/ttyACM0 at 9600 baud and waits 2 seconds for the board to
           reset. The loop sleeps Loop Delay seconds (0.05 if the field is
           empty). Extended mode uses the clock minute as the schedule key;
           short mode quantizes elapsed time to multiples of Loop Delay.
           When that key changes, "Minute changed" is printed, and if the key
           is in the schedule every character of its value is written. The
           loop ends when self.recording becomes false, then the port is closed.
           Runs on a daemon thread.
        E: A failure to open the port is caught, shown in an error dialog
           (posted to the Tk thread), and not re-raised. A non-numeric Loop
           Delay raises ValueError from float() and is not caught. Write
           errors on the open port propagate.
        R: None.
        P: None. Reads self.scheduled_start, self.recording, self.extended_assay, and the stimulus field.
        """
        try:
            arduino = serial.Serial('/dev/ttyACM0', 9600, timeout=1)
            time.sleep(2)
        except Exception as e:
            self.master.after(0, lambda m=str(e): messagebox.showerror("Error", f"Could not connect to Arduino: {m}"))
            return

        self.signal_list = self.signal_input.get().split(":")[0] if self.signal_input.get() else ""
        self.signal_delay = float(self.signal_input.get().split(":")[1]) if self.signal_input.get() else 0
        round_time = -1
        arduino_start = time.perf_counter()
        delay = 0.05
        stim_dic = self.build_stim_dic()
        last_time = time.perf_counter()
        recording_tracker = 0

        # Fire once per new key, not once per loop iteration.
        while recording_tracker < 2:
            if self.extended_assay.get():
                current_round_time = datetime.datetime.now().hour + datetime.datetime.now().minute / 100.0
            else:
                current_round_time = round(int((time.perf_counter() - arduino_start) / delay) * delay, 2)
            if round_time != current_round_time:
                round_time = current_round_time
                if round_time in stim_dic:
                    for serial_char in stim_dic[round_time]:
                        print(str(round_time) + " " + serial_char)
                        arduino.write(serial_char.encode('ascii'))
                        if serial_char in self.signal_tracker:
                            self.signal_tracker.remove(serial_char)
                        else:
                            self.signal_tracker.add(serial_char)
            if len(set(self.signal_list) & self.signal_tracker) == 1:
                last_time = self.switch_signal(arduino, self.signal_delay, last_time)
            else:
                for off_char in self.signal_list:
                    if off_char in self.signal_tracker:
                        arduino.write(off_char.encode('ascii'))
                        self.signal_tracker.remove(off_char)
                self.signal_list = self.signal_input.get().split(":")[0] ###NEWCODE
            # Update recording tracker to exit the loop after recording turns off
            if self.recording and recording_tracker == 0:
                recording_tracker = 1
            elif (not self.recording) and recording_tracker == 1:
                recording_tracker = 2
            time.sleep(delay)
        recording_tracker = 0
        self.signal_tracker.clear()
        arduino.write(self.shutdown.encode('ascii'))
        arduino.close()

    def run_arduino(self):
        """
        B: Starts send_serial on a daemon thread. The thread stops when
           self.recording is set false, and it does not keep the process alive
           after the window closes.
        E: Failure to start the thread is caught, shown in an error dialog
           (posted to the Tk thread), and not re-raised.
        R: None.
        P: None.
        """
        try:
            self.arduino_thread = threading.Thread(target=self.send_serial, daemon=True)
            self.arduino_thread.start()
        except Exception as e:
            self.master.after(0, lambda m=str(e): messagebox.showerror("Error", f"Could not start Arduino thread: {m}"))

    def select_folder(self):
        """
        B: Asks for the directory that will hold this session's files.
           Cancelling the dialog leaves the previous path in place.
        E: None.
        R: None. Writes the chosen path into self.save_path.
        P: None.
        """
        folder = filedialog.askdirectory()
        if folder:
            self.save_path.set(folder)

    def get_filename(self):
        """
        B: Builds the path fragment shared by this session's video and CSV from
           the chamber number, animal tag, and local timestamp. The leading
           slash makes it a suffix of the folder chosen above.
        E: None.
        R: String like "/Chamber1_TagA12_2026-10-28_09-43-01", with no extension.
        P: None. Reads self.chamber_number and self.animal_tag.
        """
        now = datetime.datetime.now()
        return f"/Chamber{self.chamber_number.get()}_Tag{self.animal_tag.get()}_{now.strftime('%Y-%m-%d_%H-%M-%S')}"

    def toggle_record(self):
        """
        B: Starts a recording, or requests that the current one stop.
           Starting requires a folder, an integer duration, and a free camera.
           It sets self.recording, launches record_with_preview on a daemon
           thread, and relabels the button "Stop". Pressing Stop clears
           self.recording so the camera loop and the Arduino loop both exit.
           The button label returns to "Record & Analyze" when the camera
           thread finishes.
        E: A missing folder, a non-integer duration, or a busy camera is shown
           in an error dialog and the recording is not started. ValueError is
           caught and not re-raised.
        R: None.
        P: None. Reads self.recording, self.busy, self.save_path, and self.record_duration.
        """
        if not self.recording:
            if not self.save_path.get():
                messagebox.showerror("Error", "Please select a folder path first.")
                return
            try:
                int(self.record_duration.get())
                self.recording = True
                self.record_thread = threading.Thread(target=self.record_with_preview, daemon=True)
                self.record_thread.start()
                self.record_button.config(text='Stop')
            except ValueError:
                messagebox.showerror("Error", "Please enter a valid integer for duration.")
        else:
            self.recording = False
            self.record_button.config(text='Record & Analyze')

    def record_with_preview(self):
        """
        B: Records H264 for the requested duration, remuxes to MP4, then scores
           motion. Extended sets the camera and the ffmpeg rate to 2 fps;
           otherwise both are 30. A standalone preview camera is closed first.
           The video configuration is 1456x1088 with auto exposure off, the
           exposure time from self.shutter, analogue gain 1.0, and the frame
           duration pinned to the frame rate. The sensor is monochrome, so
           there is no white balance to set. The Arduino thread starts with
           the recording. The preview window opens first (the camera itself
           starts when recording begins). The loop sleeps 0.1 s at a time
           until Stop is pressed or the duration elapses (timed from the
           scheduled start, if one is set). The camera is closed before ffmpeg
           stream-copies the H264 into an MP4 stamped at self.frame_rate.
           process_video runs only when that MP4 exists. Widget updates are
           posted to the Tk thread with master.after. Clearing self.recording
           also ends send_serial before the remux. self.busy is True while the
           camera is held.
        E: Failures are caught and shown in an error dialog; the camera, busy
           flag, recording flag, and button are always reset.
        R: None. Writes "<stem>.h264", "<stem>.mp4", and the analysis outputs.
        P: None. Reads the form fields, self.extended_assay, and self.shutter.
        """
        save_dir = self.save_path.get()
        filename = save_dir + self.get_filename()
        fileType = ".mp4"
        self.frame_rate = 2 if self.extended_assay.get() else 30

        try:
            with Picamera2() as camera:
                # Controls in the configuration are applied when the camera starts.
                camera.configure(camera.create_video_configuration(
                    main={"size": (1456, 1088)},
                    lores={"size": (320, 240)},
                    display="lores",
                    controls={
                        "FrameDurationLimits": (1000000 // self.frame_rate,) * 2,
                        "AeEnable": False,
                        "ExposureTime": int(self.shutter),
                        "AnalogueGain": 1.0,
                    }))
                #camera.zoom = (0.39, 0.3, 0.25, 0.25)

                self.run_arduino()
                #time.sleep(2)

                camera.start_preview(Preview.QT)

                while (self.recording and self.scheduled_start.get() != "" and
                       round((datetime.datetime.now().hour +
                              datetime.datetime.now().minute/100.0), 2) != float(self.scheduled_start.get())):
                    time.sleep(0.1)

                filename = save_dir + self.get_filename()
                camera.start_and_record_video("{0}.h264".format(filename))
                self.master.after(0, lambda: self.info_label.config(text=f"Recording to {filename}"))

                start_time = time.monotonic()
                while self.recording and time.monotonic() - start_time < int(self.record_duration.get()):
                    time.sleep(0.1)

                camera.stop_recording()
                camera.stop_preview()
                # Clearing this also ends send_serial before the remux and analysis.
                self.recording = False

            msg = f"Recording ended."
            self.master.after(0, lambda: self.info_label.config(text=msg))
            # Stream copy. -framerate stamps the raw H264, which has no timestamps.
            #command = f"ffmpeg -y -framerate {self.frame_rate} -i {filename}.h264 -c copy {filename}.mp4"
            command = f'MP4Box -fps {self.frame_rate} -add "{filename}.h264" -new "{filename}.mp4"'
            call([command], shell=True)

            if os.path.exists(filename + fileType) and self.processing_on:
                self.process_video(filename + fileType, f"{filename}_motion_indices.csv")

        except Exception as e:
            self.master.after(0, lambda m=str(e): messagebox.showerror("Error", f"Recording failed: {m}"))
        finally:
            self.recording = False
            self.master.after(0, lambda: self.record_button.config(text='Record & Analyze'))

    def find_plate_corners(self, gray):
        """
        B: Finds largest quadrilateral object in frame (e.g. the well plate)
           Sets self.plate_corners and returns a BGR copy of the frame with any
           detection overlay. The frame is already single-channel (the sensor
           is monochrome), so there is no colour conversion on the way in.
           Manual mode copies TL, TR, BR, BL from the eight entries and does
           no detection. Otherwise the frame is blurred and contrast-stretched.
           Canny runs hysteresis thresholds at fractions of the image's Otsu value.
           The contours are painted back on with enhanced sickness and a 7x7 kernel
           closes the gaps. A second Canny (20, 50) produces the contours that are
           scored. Largest first: the convex hull is drawn red, its polygon
           green, and every contour blue, on the BGR copy. The first polygon
           with 4 vertices is ordered and saved. If none qualifies, the
           existing corners are left in place.
        E: cv2.error if gray is not a single-channel 8-bit image on the
           automatic path. A missed plate is not an exception; the method
           prints "Failed to find corners" and returns the overlay copy.
           Manual mode does not validate that the entries form a usable quad.
        R: BGR image the same size as gray. Automatic mode has the overlay
           drawn on it; manual mode returns the plain frame.
        P: gray — single-channel uint8 frame. Not modified.
        """
        overlay = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        if self.manual_cal.get():
            self.plate_corners = np.float32([[self.tl_x.get(), self.tl_y.get()], [self.tr_x.get(), self.tr_y.get()], [self.br_x.get(), self.br_y.get()], [self.bl_x.get(), self.bl_y.get()]])
            return overlay
        blurred = cv2.GaussianBlur(gray, (15,15), 0)
        # alpha > 1 stretches contrast so the plate rim separates from the bench.
        blurred = cv2.convertScaleAbs(blurred, alpha=1.5, beta=0)

        # Otsu supplies the Canny thresholds; the binary image itself is discarded.
        otsu_thresh, _ = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        edged1 = cv2.Canny(blurred, int(otsu_thresh/6), int(otsu_thresh/2))

        contours1, _ = cv2.findContours(edged1.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        # Thick white redraw turns a broken rim into a solid band for the close.
        cv2.drawContours(edged1, contours1, -1, 255, 10)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7,7))
        closed_mask = cv2.morphologyEx(edged1, cv2.MORPH_CLOSE, kernel)

        edged2 = cv2.Canny(closed_mask, 20, 50)
        contours2, _ = cv2.findContours(edged2.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours2, key=cv2.contourArea, reverse=True)
        for c in contours:
            convex = cv2.convexHull(c)
            peri = cv2.arcLength(c, True)
            # 2% of the perimeter: tight enough to keep corners, loose enough for a rim.
            approx = cv2.approxPolyDP(convex, 0.02*peri, True)
            cv2.drawContours(overlay, [convex], -1, (0, 0, 255), 4)
            cv2.drawContours(overlay, [approx], -1, (0, 255, 0), 4)
            cv2.drawContours(overlay, contours, -1, (255, 0, 0), 4)

            if len(approx) == 4:
                # (4, 1, 2) from approxPolyDP -> (4, 2) for getPerspectiveTransform.
                pts = approx.reshape(4, 2).astype(np.float32)
                print("Successfully found plate corners.")
                self.plate_corners = self.order_points(pts)
                print(self.plate_corners)
                return overlay

        # Detection missed. Callers keep using the corners already on self.
        print("Failed to find corners")
        return overlay

    def order_points(self, pts):
        """
        B: Sorts four points into top-left, top-right, bottom-right, bottom-left.
           Top-left has the smallest x+y and bottom-right the largest. Top-right
           has the smallest y-x (np.diff along x, y) and bottom-left the largest.
           That order is what apply_warp passes to getPerspectiveTransform.
        E: None when pts has shape (4, 2). A shorter array raises IndexError
           or ValueError from the NumPy index operations.
        R: float32 array of shape (4, 2) in TL, TR, BR, BL order.
        P: pts — four (x, y) corners, shape (4, 2).
        """
        rect = np.zeros((4, 1, 2), dtype="float32")

        # Top-left has the smallest sum; bottom-right has the largest sum
        s = pts.sum(axis=1)
        rect[0] = pts[np.argmin(s)]
        rect[2] = pts[np.argmax(s)]

        # Top-right has the smallest difference; bottom-left has the largest difference
        diff = np.diff(pts, axis=1)
        rect[1] = pts[np.argmin(diff)]
        rect[3] = pts[np.argmax(diff)]

        return rect.reshape(4, 2)

    def process_video(self, video_path, output_csv_path):
        """
        B: Scores motion in each well and writes a CSV plus a mask video.
           The first decoded frame is the subtraction reference and is not a
           CSV row. OpenCV decodes the MP4 as three identical channels (the
           sensor is monochrome), so each frame is reduced to one channel
           once and everything after that is single-channel. Each later frame
           is warped, subtracted, and thresholded at 25. A well's motion index
           is the count of white pixels in its box (cv2.countNonZero). Wells
           are inset by pmarg_x and pmarg_y; the step is
           (target - 2*margin) / 8 rows and / 12 columns. Time (s) is
           frame_idx / self.frame_rate, not the file's reported fps and not
           the wall-clock duration. The header is Frame, Time (s), then
           A1..H12. The mask video is XVID, single-channel, at
           self.frame_rate and is named "<video>_analysis.avi".
        E: A video that cannot be opened prints a message and returns. A file
           with no first frame returns after the capture is released. Neither
           case raises. CSV or VideoWriter failures propagate.
        R: None.
        P: video_path — MP4 to read. output_csv_path — motion-index CSV to create.
        """
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print(f"Error opening video stream for analysis: {video_path}")
            return

        fps = self.frame_rate
        ret, prev_frame = cap.read()
        if not ret:
            cap.release()
            return

        prev_gray = cv2.cvtColor(prev_frame, cv2.COLOR_BGR2GRAY)
        prev_warp = self.apply_warp(prev_gray)

        # 8 rows (A-H) by 12 columns, inset by the margins. The outer edge of
        # the last well lands on target - margin, inside the warped image.

        row_step = (self.target_height - 2*self.pmarg_y) / 8.0
        col_step = (self.target_width - 2*self.pmarg_x) / 12.0
        well_boxes = []
        for r in range(8):
            for c in range(12):
                x1 = int(self.pmarg_x + c * col_step)
                y1 = int(self.pmarg_y + r * row_step)
                x2 = int(self.pmarg_x + (c + 1) * col_step)
                y2 = int(self.pmarg_y + (r + 1) * row_step)
                well_boxes.append((r, c, x1, y1, x2, y2))

        with open(output_csv_path, mode='w', newline='') as csv_file:
            writer = csv.writer(csv_file)
            header = ['Frame', 'Time (s)']
            for r, c, _, _, _, _ in well_boxes:
                header.append(f"{chr(65+r)}{c+1}")
            writer.writerow(header)

            # Binary motion mask, written beside the source MP4. Single-channel,
            # so frames go to the writer as they are with no BGR conversion.
            fourcc = cv2.VideoWriter_fourcc(*'XVID')
            analysis_path = video_path.replace(".mp4", ".avi")
            analysis_writer = cv2.VideoWriter(analysis_path, fourcc, self.frame_rate, (self.target_width,self.target_height), isColor=False)

            frame_idx = 0
            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break

                frame_idx += 1
                current_time = frame_idx / fps

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                warp_frame = self.apply_warp(gray)

                # Pixels that changed by more than 25 gray levels become the mask.
                diff = cv2.absdiff(warp_frame, prev_warp)
                _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)

                analysis_writer.write(thresh)

                row_data = [frame_idx, round(current_time, 3)]
                for r, c, x1, y1, x2, y2 in well_boxes:
                    motion_index = cv2.countNonZero(thresh[y1:y2, x1:x2])
                    row_data.append(motion_index)

                writer.writerow(row_data)
                prev_warp = warp_frame
        analysis_writer.release()
        cap.release()
        self.master.after(0, lambda: self.info_label.config(text=f"Analysis complete! Saved: {os.path.basename(output_csv_path)}"))

    def capture_and_display(self):
        """
        B: Grabs one still at 1456x1088 and shows it in a 640x400 popup. The
           file is <save folder>/image.jpg. A standalone preview camera is
           closed first. The camera runs for one second before the capture so
           auto exposure can settle. When Manual is on, a 50-pixel grid
           is drawn on that still first (colour lines, so the still is read
           back as BGR) and the popup title says the grid is shown. Exposure
           is left on auto. Runs on the Tk thread.
        E: Capture and display failures are caught, shown in an error dialog,
           and not re-raised. A busy camera is reported the same way.
        R: None.
        P: None. Reads self.save_path and self.manual_cal.
        """
        try:
            # Free a standalone preview camera so a second Picamera2 can open.
            with Picamera2() as camera:
                image_path = self.save_path.get() + '/image.jpg'
                camera.configure(camera.create_still_configuration(main={"size": (1456, 1088)}))
                camera.start()
                time.sleep(1)    # let auto exposure settle
                camera.capture_file(image_path)
            # Drawing grid overlay for coordinate finding in manual calibration mode
            if self.manual_cal.get():
                grid_img = cv2.imread(image_path)
                step_size = 50
                for y in range(6):
                    color = (0, 255-30*y, 30*y)
                    cv2.line(grid_img, (0, y*step_size), (1456, y*step_size), color, 4)
                    cv2.line(grid_img, (0, 1088 - y*step_size), (1456, 1088 - y*step_size), color, 4)
                for x in range(6):
                    color = (0, 255-40*x, 40*x)
                    cv2.line(grid_img, (x*step_size, 0), (x*step_size, 1088), color, 4)
                    cv2.line(grid_img, (1456 - x*step_size, 0), (1456 - x*step_size, 1088), color, 4)
                cv2.imwrite(image_path, grid_img)
            pil_img = Image.open(image_path).resize((640,400), Image.LANCZOS)
            popup = tk.Toplevel(self.master)
            popup.title("Captured Image" + (" (Grid Shown)" if self.manual_cal.get() else " (No Grid)"))
            tk_img = ImageTk.PhotoImage(pil_img)
            popup_label = ttk.Label(popup, image=tk_img)
            popup_label.image = tk_img  # Keep reference to prevent garbage collection!
            popup_label.pack(padx=10, pady=10)
        except Exception as e:
            messagebox.showerror("Error", f"Could not capture image: {e}")

    def toggle_calibrate(self):
        """
        B: Starts a recording, or requests that the current one stop.
           Starting requires a folder, an integer duration, and a free camera.
           It sets self.recording, launches record_with_preview on a daemon
           thread, and relabels the button "Stop". Pressing Stop clears
           self.recording so the camera loop and the Arduino loop both exit.
           The button label returns to "Record & Analyze" when the camera
           thread finishes.
        E: A missing folder, a non-integer duration, or a busy camera is shown
           in an error dialog and the recording is not started. ValueError is
           caught and not re-raised.
        R: None.
        P: None. Reads self.recording, self.busy, self.save_path, and self.record_duration.
        """
        if not self.calibrating:
            if not self.save_path.get():
                messagebox.showerror("Error", "Please select a folder path first.")
                return
            try:
                self.calibrating = True
                self.calibrate_thread = threading.Thread(target=self.calibrate_plate, daemon=True)
                self.calibrate_thread.start()
                self.calibrate_button.config(text = 'Finish Calibration')
            except Exception as e:
                messagebox.showerror("Error", f"Failed to start plate calibration: {e}")
        else:
            self.calibrating = False
            self.calibrate_button.config(text='Capture and Calibrate Plate')

    def calibrate_plate(self):
        """
        B: Samples the shutter speed, detects the plate, and previews the 8x12
           grid. A standalone preview camera is closed first. The camera
           starts at 1456x1088 with auto exposure on and the live preview
           showing; after 2 seconds the last frame's exposure time is read
           from the camera metadata, stored on self, and pinned (auto
           exposure off, analogue gain 1.0). The still is taken when the
           operator unticks Preview, and is read back as a single-channel
           image. Corner detection draws on a BGR copy of that frame (or
           copies the manual entries); the result is warped and a 9-by-13
           line grid is drawn on top. Outer boundary lines are orange; inner
           lines are green. The annotated, unwarped frame is what is saved
           and shown in the popup; both temporary JPEGs are deleted. The popup
           and status text are created on the Tk thread via master.after.
           Recording later uses this shutter.
        E: Capture, load, and preview failures are caught, shown in an error
           dialog, and not re-raised. The camera and busy flag are always reset.
        R: None. On the success path, self.plate_corners and self.shutter are updated.
        P: None. Reads self.save_path and the plate geometry fields.
        """
        image_path = self.save_path.get() + '/calibration_capture.jpg'
        preview_path = self.save_path.get() + '/calibrated_plate_preview.jpg'
        try:
            # Free a standalone preview camera so a second Picamera2 can open.
            with Picamera2() as camera:
                # Auto exposure is on by default.
                camera.configure(camera.create_preview_configuration(
                    main={"size": (1456, 1088)},
                    lores={"size": (320, 240)},
                    display="lores",
                    controls={"FrameDurationLimits": (1000000 // self.frame_rate,) * 2}))
                #camera.zoom = (0.39, 0.3, 0.25, 0.25)
                camera.start_preview(Preview.QT)
                camera.start()
                # Let auto exposure settle, then pin the shutter.
                time.sleep(2)
                metadata = camera.capture_metadata()
                self.shutter = metadata["ExposureTime"]
                camera.set_controls({
                    "AeEnable": False,
                    "ExposureTime": self.shutter,
                    "AnalogueGain": 1.0,
                })
                print(self.shutter)
                print(metadata["AnalogueGain"])      # gain auto exposure chose; recording locks 1.0

                while self.calibrating:
                    time.sleep(0.1)

                camera.capture_file(image_path)
                print("Captured")

            gray = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
            if gray is None:
                self.master.after(0, lambda: messagebox.showerror("Error", "Failed to load calibration image."))
                return

            overlay = self.find_plate_corners(gray)
            warped = self.apply_warp(overlay)

            # Inset grid: 8 rows need 9 lines, 12 columns need 13. The step is
            # (target - 2*margin) so the outer lines sit margin pixels inward.
            row_step = (self.target_height-2*self.pmarg_y) / 8.0
            col_step = (self.target_width-2*self.pmarg_x)/ 12.0

            for r in range(9):
                y = int(r * row_step) + self.pmarg_y
                cv2.line(warped, (0, y), (self.target_width, y), (0, 150, 255 if r%8==0 else 0), 2)

            for c in range(13):
                x = int(c * col_step) + self.pmarg_x
                cv2.line(warped, (x, 0), (x, self.target_height), (0, 150, 255 if c%12==0 else 0), 2)

            cv2.imwrite(preview_path, overlay) #####warped
            pil_img = Image.open(preview_path).resize((640,400), Image.LANCZOS)

            for path in (image_path, preview_path):
                if os.path.exists(path):
                    os.remove(path)
                else:
                    print(f"Failed to delete {path}")
            print("Image processed")
            # Widgets must be created on the Tk thread.
            def show_popup():
                popup = tk.Toplevel(self.master)
                popup.title("Plate Calibration Preview")
                tk_img = ImageTk.PhotoImage(pil_img)
                popup_label = ttk.Label(popup, image=tk_img)
                popup_label.image = tk_img  # Keep reference to prevent garbage collection!
                popup_label.pack(padx=10, pady=10)
                self.info_label.config(text="Calibration preview generated with grid overlay.")
            self.master.after(0, show_popup)

        except Exception as e:
            self.master.after(0, lambda m=str(e): messagebox.showerror("Error", f"Calibration capture failed: {m}"))
        finally:
            camera.close()
def main():
    """
    B: Opens the EZMice window and runs until it is closed. Creates the Tk
    root, constructs EZMiceApp on it, and enters mainloop.
    E: Tk errors during startup propagate.
    R: None.
    P: None.
    """
    root = tk.Tk()
    app = EZMiceApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
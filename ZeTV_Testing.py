#!/usr/bin/env python3
"""
EZMice v0.4.1 — well-plate recording and per-well motion scoring.

B: Records a plate assay, pulses an Arduino from a stimulus schedule, and
   writes a motion index for each of the 96 wells. Short assays run at 30 fps;
   Extended assays run at 2 fps and key the Arduino off the clock minute.
   PiCamera records H264, ffmpeg remuxes it at that frame rate, and OpenCV
   warps the plate before frame-subtraction. Calibration can lock white
   balance and shutter speed, or the operator can type the four corners.
E: None at import. Each method reports its own failures.
R: None. Runs until the window is closed.
P: None. Start with `python3 ezmice.py`.
"""

from asyncio import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk
import threading
import datetime

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
       (white balance, shutter, frame rate), the Arduino thread, and the
       plate geometry. Button callbacks start work; serial output and
       recording run on daemon threads.
    E: None from construction. See each method for the errors it reports.
    R: None. Keep the instance alive for as long as master.mainloop() runs.
    P: master — the Tk root this UI is gridded onto.
    """

    def __init__(self, master):
        """
        B: Stores blank form fields, default plate geometry, and camera
           settings, then builds the window. Extended defaults off, so
           frame_rate starts at 30 and is rewritten from the checkbox when
           a recording starts. Plate corners default to a rectangle inside a
           1296x972 frame; the warp target is 600x400 with margins 48 and 34.
           Manual corners default to TL (150, 100), TR (1150, 100),
           BL (150, 800), BR (1150, 800). awbgains and shutter are replaced
           when Capture & Calibrate samples the camera.
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
        # 0 = short assay at 30 fps, 1 = extended assay at 2 fps.
        self.extended_assay = tk.IntVar() # 0 "short" or 1 "extended"
        # Stimulus schedule text ("time:chars,...") and the serial tick length.
        self.stim_set = tk.StringVar()
        self.scheduled_start = tk.StringVar()
        self.shutdown = 'c'
        self.signal_tracker = set()

        # Source-image corners (TL, TR, BR, BL) and the rectangle they warp into.
        self.plate_corners = np.float32([[100, 100], [1196, 100], [1196, 872], [100, 872]])
        self.target_width = 600
        self.target_height = 400

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
        self.pmarg_x = 48
        self.pmarg_y = 34

        # Red and blue gains. Calibration overwrites this after sampling auto AWB.
        self.awbgains = (1.0, 2.3)
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
        ttk.Label(self.master, text='Scheduled Start Time (HH.MM):').grid(row=5, column=0, sticky='w', padx=5, pady=2)

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

        # Seconds between serial time checks. Empty means 0.05 in send_serial.
        self.start_entry = ttk.Entry(self.master, textvariable=self.scheduled_start)
        self.start_entry.grid(row=5, column=1, sticky='w', padx=5, pady=2)

        self.record_button = ttk.Button(self.master, text='Record & Analyze', command=self.toggle_record)
        self.record_button.grid(row=6, column=0, sticky='ew', padx=5, pady=5)

        ttk.Button(self.master, text='Capture Image', command=self.capture_and_display).grid(row=6, column=2, sticky='ew', padx=5, pady=5)
        ttk.Button(self.master, text='Capture & Calibrate Plate', command=self.capture_and_calibrate).grid(row=7, column=2, sticky='ew', padx=5, pady=5)

        self.info_label = ttk.Label(self.master, text='')
        self.info_label.grid(row=7, column=0, columnspan=2, padx=5, pady=5)

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

        # Color mapping for stimulus / solenoid overlay (BGR format)
        self.stim_colors = {
            'w': (255, 255, 255),  # White
            'v': (211, 0, 148),    # Violet (approximate BGR)
            'x': (200, 200, 200),  # Auxiliary White (Light Gray)
            'r': (0, 0, 255),      # Red
            'g': (0, 255, 0),      # Green
            's': (255, 165, 0),    # Soft tap solenoid (Blue/Orange-ish or Cyan)
            'h': (0, 69, 255)      # Hard tap solenoid (Deep Orange / Red-Orange)
        }

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
                entry.grid(row=int(i/4 - 1 + 9), column=int(i%4), padx=5, pady=2)
            else:
                entry.grid_remove()

    def apply_warp(self, img):
        """
        B: Stretches the calibrated plate so it fills the target rectangle.
           Maps self.plate_corners, ordered TL-TR-BR-BL, onto (0, 0),
           (target_width, 0), (target_width, target_height), (0, target_height).
           Pixels outside the quad are dropped.
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
    
    def draw_status_overlay(self, frame):
        """
        B: Draws a status HUD overlay on the frame showing active stimulus/solenoid states.
        E: None.
        R: Frame with overlay rendered.
        P: frame - BGR image to draw on.
        """
        # Background panel for HUD
        overlay = frame.copy()
        h_f, w_f = frame.shape[:2]
        
        # Draw a semi-transparent header box or sidebar tracker
        cv2.rectangle(overlay, (10, 10), (320, 50 + len(self.stim_colors) * 25), (30, 30, 30), -1)
        cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
        
        # Title
        cv2.putText(frame, "STIMULUS & SOLENOID STATUS", (20, 30), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
        
        # Legend labels and live active status indicators
        labels = {
            'w': 'White Light (w)',
            'v': 'Violet Light (v)',
            'x': 'Aux White (x)',
            'r': 'Red Light (r)',
            'g': 'Green Light (g)',
            's': 'Soft Tap Solenoid (s)',
            'h': 'Hard Tap Solenoid (h)'
        }
        
        y_offset = 55
        for key, label_text in labels.items():
            is_active = key in self.signal_tracker
            color = self.stim_colors.get(key, (255, 255, 255))
            
            # Draw indicator circle (Filled if active, hollow ring if inactive)
            circle_color = color if is_active else (80, 80, 80)
            thickness = -1 if is_active else 1
            cv2.circle(frame, (35, y_offset - 4), 8, circle_color, thickness)
            cv2.circle(frame, (35, y_offset - 4), 8, (200, 200, 200), 1) # border ring
            
            # Draw text label
            text_color = (255, 255, 255) if is_active else (150, 150, 150)
            cv2.putText(frame, label_text, (55, y_offset), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, text_color, 1, cv2.LINE_AA)
            
            y_offset += 25
            
        return frame
    
    def build_stim_dic(self):
        """
        B: Turns the Stimulus Set field into {time_key: command_characters}.
           An empty field selects a built-in schedule. Extended mode keys that
           schedule by clock minute, every two minutes from now through
           now+10, each value "w". Short mode uses elapsed seconds:
           {1: "w", 2: "wr", 3: "rg", 4: "gv", 5: "vs", 6: "sh", 7: "h"}.
           A non-empty field is comma-separated "time:chars" items in either
           mode. Characters for the same time are concatenated; each is later
           written as one ASCII byte.
        E: A missing colon or a non-numeric time is caught, shown in an error
           dialog, and not re-raised. The dict parsed before that item is returned.
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
            messagebox.showerror("Error", f"Stimulus set input error: {e}")
        return stim_dic
    def switch_signal(self, duration, last_time):
        """
        B: Switches the specified signals for a given duration.
        E: None.
        R: None.
        P: None.
        """
        if time.perf_counter() - last_time >= duration:
            last_time = time.perf_counter()
            #signal_list.append(signal_list.pop(0))
            #Last list item is the signal currently "on",
            #first list item is next one to be turned "on"
            print(self.signal_list[0].encode('ascii'))
            self.signal_tracker.add(self.signal_list[0])
            print(self.signal_list[len(self.signal_list) - 1].encode('ascii'))
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
        E: A failure to open the port is caught, shown in an error dialog, and
           not re-raised. A non-numeric Loop Delay raises ValueError from float()
           and is not caught. Write errors on the open port propagate.
        R: None.
        P: None. Reads self.scheduled_start, self.recording, self.extended_assay, and the stimulus field.
        """
        try:
            #arduino = serial.Serial('/dev/ttyACM0', 9600, timeout=1)
            time.sleep(2)
        except Exception as e:
            messagebox.showerror("Error", f"Could not connect to Arduino: {e}")
            return

        round_time = -1
        arduino_start = time.perf_counter()
        delay = 0.05
        stim_dic = self.build_stim_dic()
        last_time = time.perf_counter()
        recording_tracker = 0
        self.signal_list = "xw"

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
                        print(round_time)
                        print(serial_char.encode('ascii'))
                        if serial_char in self.signal_tracker:
                            self.signal_tracker.remove(serial_char)
                        else:
                            self.signal_tracker.add(serial_char)
            if len(set(self.signal_list) & self.signal_tracker) == 1:
                last_time = self.switch_signal(2, last_time)
            else:
                for serial_char in self.signal_list:
                    if serial_char in self.signal_tracker:
                        print(serial_char.encode('ascii'))
                        self.signal_tracker.remove(serial_char)
            # Update recording tracker to exit the loop after recording turns off
            if (self.recording) and recording_tracker == 0:
                recording_tracker = 1
            elif (not self.recording) and recording_tracker == 1:
                recording_tracker = 2
            time.sleep(delay)
        recording_tracker = 0
        print(self.shutdown.encode('ascii'))
        #arduino.close()

    def run_arduino(self):
        """
        B: Starts send_serial on a daemon thread. The thread stops when
           self.recording is set false, and it does not keep the process alive
           after the window closes.
        E: Failure to start the thread is caught, shown in an error dialog, and
           not re-raised.
        R: None.
        P: None.
        """
        try:
            self.arduino_thread = threading.Thread(target=self.send_serial, daemon=True)
            self.arduino_thread.start()
        except Exception as e:
            messagebox.showerror("Error", f"Could not start Arduino thread: {e}")

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
        R: String like "/Chamber1_TagA12_20261028_094301", with no extension.
        P: None. Reads self.chamber_number and self.animal_tag.
        """
        now = datetime.datetime.now()
        return f"/Chamber{self.chamber_number.get()}_Tag{self.animal_tag.get()}_{now.strftime('%Y-%m-%d_%H:%M:%S')}"

    def toggle_record(self):
        """
        B: Starts a recording, or requests that the current one stop.
           Starting requires a folder and an integer duration. It sets
           self.recording, launches record_with_preview on a daemon thread,
           and relabels the button "Stop". Pressing Stop clears self.recording
           so the camera loop and the Arduino loop both exit. The button label
           returns to "Record & Analyze" when the camera thread finishes.
        E: A missing folder or a non-integer duration is shown in an error
           dialog and the recording is not started. ValueError is caught and
           not re-raised.
        R: None.
        P: None. Reads self.recording, self.save_path, and self.record_duration.
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
        save_dir = self.save_path.get()
        fileType = ".mp4"
        self.frame_rate = 2 if self.extended_assay.get() else 30

        cap = cv2.VideoCapture(0)
        cv2.namedWindow("Camera Preview", cv2.WINDOW_NORMAL)

        try:
            self.run_arduino()
            time.sleep(2)

            # Preview loop while waiting for scheduled start time
            while self.recording and self.scheduled_start.get() != "":
                now = datetime.datetime.now()
                current_hhmm = now.hour + (now.minute / 100.0)
                if current_hhmm == float(self.scheduled_start.get()):
                    break

                ret, frame = cap.read()
                if ret:
                    cv2.imshow("Camera Preview", frame)
                cv2.waitKey(1)
                time.sleep(0.05)

            # Recompute filename right before recording starts
            filename = save_dir + self.get_filename()
            mp4_path = f"{filename}{fileType}"

            # OpenCV writes directly to MP4 (replaces .h264 recording + ffmpeg remux)
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            out = cv2.VideoWriter(mp4_path, fourcc, self.frame_rate, (1296, 972))

            self.master.after(0, lambda: self.info_label.config(text=f"Recording to {filename}"))

            # Reset start_time right when recording begins
            start_time = time.monotonic()
            duration = int(self.record_duration.get())

            # Main preview + recording loop (replaces wait_recording)
            while self.recording and (time.monotonic() - start_time < duration):
                ret, frame = cap.read()
                if not ret:
                    break
                # Apply live overlay to preview
                display_frame = self.draw_status_overlay(frame.copy())

                out.write(frame)  # Write raw or with overlay depending on your preference
                cv2.imshow("Camera Preview", display_frame)
                cv2.waitKey(1)

            # Replaces stop_recording()
            out.release()

            if self.recording:
                self.master.after(0, lambda: self.info_label.config(text=f"Video saved to {filename}"))
            else:
                self.master.after(0, lambda: self.info_label.config(text="Recording stopped."))

        finally:
            # Clearing this also ends send_serial before analysis
            self.recording = False
            # Replaces camera.close() and camera.stop_preview()
            cap.release()
            cv2.destroyAllWindows()
            self.master.after(0, lambda: self.record_button.config(text='Record & Analyze'))

        if os.path.exists(mp4_path):
            self.process_video(mp4_path, f"{filename}_motion_indices.csv")

    def find_plate_corners(self, img):
        """
        B: Sets self.plate_corners and returns img with any detection overlay.
           Manual mode copies TL, TR, BR, BL from the eight entries and does
           no detection. Otherwise the grayscale frame is blurred and
           contrast-stretched. Canny runs at 1/12 and 1/5 of that image's Otsu
           value. Those contours are painted back on, 20 px thick, and a 7x7
           close joins gaps. A second Canny (20, 50) produces the contours
           that are scored. Largest first: the convex hull is drawn red, its
           polygon green, and every contour blue. The first polygon with 4
           vertices is ordered and saved. If none qualifies, the existing
           corners are left in place.
        E: OpenCV raises if img is not a BGR image on the automatic path.
           A missed plate is not an exception; the method prints
           "Failed to find corners" and returns img. Manual mode does not
           validate that the entries form a usable quad.
        R: img. Automatic mode draws the overlay on it; manual mode returns it unchanged.
        P: img — BGR frame. Modified in place on the automatic path.
        """
        if self.manual_cal.get():
            self.plate_corners = np.float32([[self.tl_x.get(), self.tl_y.get()], [self.tr_x.get(), self.tr_y.get()], [self.br_x.get(), self.br_y.get()], [self.bl_x.get(), self.bl_y.get()]])
            return img
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (15,15), 0) ###gray
        # alpha > 1 stretches contrast so the plate rim separates from the bench.
        blurred = cv2.convertScaleAbs(blurred, alpha=1.5, beta=0) #blurred

        # Otsu supplies the Canny thresholds; the binary image itself is discarded.
        otsu_thresh, _ = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        edged1 = cv2.Canny(blurred, int(otsu_thresh/14), int(otsu_thresh/5.5))

        contours1, _ = cv2.findContours(edged1.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        # Thick white redraw turns a broken rim into a solid band for the close.
        cv2.drawContours(edged1, contours1, -1, (255, 255, 255), 20)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7,7))
        closed_mask = cv2.morphologyEx(edged1, cv2.MORPH_CLOSE, kernel)

        edged2 = cv2.Canny(closed_mask, 20, 50)
        contours2, _ = cv2.findContours(edged2.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours2, key=cv2.contourArea, reverse=True)
        i = 0
        for c in contours:
            convex = cv2.convexHull(c)
            area = cv2.contourArea(c)
            peri = cv2.arcLength(c, True)
            # 2% of the perimeter: tight enough to keep corners, loose enough for a rim.
            approx = cv2.approxPolyDP(convex, 0.02*peri, True)
            cv2.drawContours(img, [convex], -1, (0, 0, 255), 4)
            cv2.drawContours(img, [approx], -1, (0, 255, 0), 4)
            cv2.drawContours(img, contours, -1, (255, 0, 0), 4)

            if len(approx) == 4:
                # (4, 1, 2) from approxPolyDP -> (4, 2) for getPerspectiveTransform.
                pts = approx.reshape(4, 2).astype(np.float32)
                print("Successfully found plate corners.")
                self.plate_corners = self.order_points(pts)
                print(self.plate_corners)
                return img

        # Detection missed. Callers keep using the corners already on self.
        print("Failed to find corners")
        return img

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
           CSV row. Each later frame is warped, subtracted, and thresholded
           at 25. A well's motion index is the count of white pixels in its
           box. Wells are inset by pmarg_x and pmarg_y; the step is
           (target - 2*margin) / 8 rows and / 12 columns. Time (s) is
           frame_idx / self.frame_rate, not the file's reported fps and not
           the wall-clock duration. The header is Frame, Time (s), then
           A1..H12. The mask video is XVID at 30 fps regardless of the
           capture rate, so a 2 fps assay plays back sped up. It is named
           "<video>_analysis.avi".
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
        #prev_gray = self.find_plate_corners(prev_frame)
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

            # Binary motion mask, written beside the source MP4.
            # Playback rate is fixed at 30 even when the assay was captured at 2.
            fourcc = cv2.VideoWriter_fourcc(*'XVID')
            analysis_path = video_path.replace(".mp4", ".avi")
            analysis_writer = cv2.VideoWriter(analysis_path, fourcc, self.frame_rate, (self.target_width,self.target_height))

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
                thresh_color = cv2.cvtColor(thresh, cv2.COLOR_GRAY2BGR)

                analysis_writer.write(thresh_color) ###thresh_color

                row_data = [frame_idx, round(current_time, 3)]
                for r, c, x1, y1, x2, y2 in well_boxes:
                    well_roi = thresh[y1:y2, x1:x2]
                    motion_index = np.sum(well_roi > 0)
                    row_data.append(motion_index)

                writer.writerow(row_data)
                prev_warp = warp_frame
        analysis_writer.release()
        cap.release()
        self.master.after(0, lambda: self.info_label.config(text=f"Analysis complete! Saved: {os.path.basename(output_csv_path)}"))

    def capture_and_display(self):
        try:
            image_path = os.path.join(self.save_path.get(), 'image.jpg')
            width, height = 1296, 972

            cap = cv2.VideoCapture(0)
            try:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

                # Flush a few initial frames so auto-exposure/AWB can settle
                for _ in range(5):
                    ret, frame = cap.read()

                if not ret or frame is None:
                    raise RuntimeError("Failed to grab frame from camera.")
            finally:
                cap.release()

            # Replicate camera.zoom = (0.39, 0.3, 0.25, 0.25)
            x, y = int(0.39 * width), int(0.3 * height)
            zw, zh = int(0.25 * width), int(0.25 * height)
            cropped = frame[y:y+zh, x:x+zw]

            # Upscale crop back to 1296x972 (matches picamera's output resolution & grid coordinates)
            grid_img = cv2.resize(cropped, (width, height), interpolation=cv2.INTER_LINEAR)

            # Drawing grid overlay for coordinate finding in manual calibration mode
            if self.manual_cal.get():
                step_size = 50
                for y_idx in range(6):
                    color = (0, 255 - 30 * y_idx, 30 * y_idx)
                    cv2.line(grid_img, (0, y_idx * step_size), (width, y_idx * step_size), color, 4)
                    cv2.line(grid_img, (0, height - y_idx * step_size), (width, height - y_idx * step_size), color, 4)
                for x_idx in range(6):
                    color = (0, 255 - 40 * y_idx, 40 * x_idx)
                    cv2.line(grid_img, (x_idx * step_size, 0), (x_idx * step_size, height), color, 4)
                    cv2.line(grid_img, (width - x_idx * step_size, 0), (width - x_idx * step_size, height), color, 4)

            # Save final still to disk
            cv2.imwrite(image_path, grid_img)

            # Convert BGR (OpenCV) to RGB (PIL) for Tkinter display
            rgb_img = cv2.cvtColor(grid_img, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb_img).resize((640, 400), Image.LANCZOS)

            popup = tk.Toplevel(self.master)
            popup.title("Captured Image" + (" (Grid Shown)" if self.manual_cal.get() else " (No Grid)"))
            tk_img = ImageTk.PhotoImage(pil_img)
            popup_label = ttk.Label(popup, image=tk_img)
            popup_label.image = tk_img  # Keep reference to prevent garbage collection!
            popup_label.pack(padx=10, pady=10)

        except Exception as e:
            messagebox.showerror("Error", f"Could not capture image: {e}")


    def capture_and_calibrate(self):
        try:
            image_path = os.path.join(self.save_path.get(), 'calibration_capture.jpg')
            preview_path = os.path.join(self.save_path.get(), 'calibrated_plate_preview.jpg')
            width, height = 1296, 972

            cap = cv2.VideoCapture(0)
            try:
                # Live preview loop: press Enter (13) or Space (32) in the window to capture
                cv2.namedWindow("Calibration Preview - Press SPACE or ENTER to capture", cv2.WINDOW_NORMAL)
                frame = None
                while True:
                    ret, current_frame = cap.read()
                    if ret:
                        frame = current_frame
                        cv2.imshow("Calibration Preview - Press SPACE or ENTER to capture", frame)

                    key = cv2.waitKey(1) & 0xFF
                    if key in (13, 32):  # Enter or Space key
                        break

                cv2.destroyAllWindows()

                if frame is None:
                    messagebox.showerror("Error", "Failed to capture calibration image.")
                    return

                # Save raw capture (preserving your exact disk flow)
                cv2.imwrite(image_path, frame)

            finally:
                cap.release()
                cv2.destroyAllWindows()

            img = cv2.imread(image_path)
            if img is None:
                messagebox.showerror("Error", "Failed to load calibration image.")
                return

            gray = self.find_plate_corners(img)
            warped = self.apply_warp(gray)

            # Inset grid: 8 rows need 9 lines, 12 columns need 13.
            row_step = (self.target_height - 2 * self.pmarg_y) / 8.0
            col_step = (self.target_width - 2 * self.pmarg_x) / 12.0

            for r in range(9):
                y = int(r * row_step) + self.pmarg_y
                cv2.line(warped, (0, y), (self.target_width, y), (0, 150, 255 if r % 8 == 0 else 0), 2)

            for c in range(13):
                x = int(c * col_step) + self.pmarg_x
                cv2.line(warped, (x, 0), (x, self.target_height), (0, 150, 255 if c % 12 == 0 else 0), 2)

            cv2.imwrite(preview_path, gray)  #####warped
            pil_img = Image.open(preview_path).resize((640, 400), Image.LANCZOS)

            popup = tk.Toplevel(self.master)
            popup.title("Plate Calibration Preview")

            tk_img = ImageTk.PhotoImage(pil_img)

            popup_label = ttk.Label(popup, image=tk_img)
            popup_label.image = tk_img  # Keep reference to prevent garbage collection!
            popup_label.pack(padx=10, pady=10)

            time.sleep(1)
            for path in (image_path, preview_path):
                if os.path.exists(path):
                    os.remove(path)
                else:
                    print(f"Failed to delete {path}")

            self.info_label.config(text="Calibration preview generated with grid overlay.")

        except Exception as e:
            messagebox.showerror("Error", f"Calibration capture failed: {e}")


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
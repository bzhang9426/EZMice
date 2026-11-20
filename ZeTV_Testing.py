#!/usr/bin/env python3
"""
Laptop-adapted version of ZeTV apparatus code (mirrors the Pi "extended"
build). Supports short assays and long assays. Includes manual and automatic
plate calibration modes, basic cv2 image processing, etc. Replaces serial
commands with print lines and simulated LED/solenoid visualizer. Intended for
use on Windows with native webcam.
"""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk
import threading
import datetime
import time
from subprocess import call
import os
import cv2
import numpy as np
import csv


class EZMiceApp:
    """
    B: Operator window for one chamber's recording, stimulus, and analysis on
       Windows/webcam. Holds the form fields, Extended/Manual flags, webcam
       settings, simulated Arduino thread, and plate geometry. Button callbacks
       start work; simulated serial output, recording, and calibration run on
       daemon threads. Only one task uses the webcam at a time; self.busy is
       True while recording or calibrating owns it.
    E: None from construction. See each method for the errors it reports.
    R: None. Keep the instance alive for as long as master.mainloop() runs.
    P: master (tk.Tk): The Tk root window this UI is gridded onto.
    """

    def __init__(self, master):
        """
        B: Stores blank form fields, default plate geometry, and placeholder
           camera settings, then builds the GUI window. Extended defaults off
           (frame rate = 30 fps; switches to 2 fps if checked at record time).
           Plate corners default to a rectangle inside a 1296x972 frame; the
           warp target is 600x400 with margins 48 and 34. Manual corners
           default to TL (150, 100), TR (1150, 100), BL (150, 800), BR
           (1150, 800). signal_list starts empty and is filled from the Signal
           Input field when a recording starts. processing_on is False, so
           motion analysis is skipped unless it is set to True.
        E: None.
        R: None.
        P: master (tk.Tk): Tk root window.
        """
        self.master = master
        self.master.title('EZMice v0.4.1')
        self.style = ttk.Style()
        self.style.theme_use('clam')

        self.busy = False   # True while record/calibrate owns the webcam
        self.record_duration = tk.StringVar()
        self.chamber_number = tk.StringVar()
        self.animal_tag = tk.StringVar()
        self.save_path = tk.StringVar()
        self.recording = False

        self.extended_assay = tk.IntVar()
        self.stim_set = tk.StringVar()
        self.scheduled_start = tk.StringVar()
        self.shutdown = 'c'
        self.signal_tracker = set()
        self.signal_input = tk.StringVar()
        self.signal_list = ""
        self.signal_delay = 2

        self.plate_corners = np.float32([[100, 100], [1196, 100], [1196, 872], [100, 872]])
        self.target_width = 600
        self.target_height = 400

        # Video processing toggle (no checkbox; set True to score motion).
        self.processing_on = False

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

        self.pmarg_x = 48
        self.pmarg_y = 34
        self.awbgains = (1.0, 2.3)
        self.frame_rate = 2 if self.extended_assay.get() else 30
        self.shutter = 100000

        self.create_widgets()

    def create_widgets(self):
        """
        B: Lays out the form entries, Extended and Manual checkboxes, action
           buttons, and status line. Column 0 holds labels, column 1 holds
           entries, column 2 holds Extended, Select Folder, and capture
           buttons, and column 3 holds Manual. Rows: duration, chamber, tag,
           folder, stimulus set, signal input, scheduled start, then the
           Record and Capture Image buttons, and the Calibrate button and
           status line below them. The eight manual-corner entries are created
           but hidden until toggle_coords() is triggered. Also defines the BGR
           color map (self.stim_colors) for the live HUD visualizer.
        E: None.
        R: None. Widgets updated by later methods are stored as attributes on self.
        P: None. Uses self.master and the Tk variables initialized in __init__.
        """
        ttk.Label(self.master, text='Record Duration (s):').grid(row=0, column=0, sticky='w', padx=5, pady=2)
        ttk.Label(self.master, text='Chamber Number:').grid(row=1, column=0, sticky='w', padx=5, pady=2)
        ttk.Label(self.master, text='Animal Tag:').grid(row=2, column=0, sticky='w', padx=5, pady=2)
        ttk.Label(self.master, text='Folder Path:').grid(row=3, column=0, sticky='w', padx=5, pady=2)
        ttk.Label(self.master, text='Stimulus Set:').grid(row=4, column=0, sticky='w', padx=5, pady=2)
        ttk.Label(self.master, text='Signal Input / Delay (s):').grid(row=5, column=0, sticky='w', padx=5, pady=2)
        ttk.Label(self.master, text='Scheduled Start Time (HH.MM):').grid(row=6, column=0, sticky='w', padx=5, pady=2)

        ttk.Checkbutton(self.master, text="Extended", variable=self.extended_assay).grid(row=0, column=2, padx=5, pady=2)

        self.duration_entry = ttk.Entry(self.master, textvariable=self.record_duration)
        self.duration_entry.grid(row=0, column=1, padx=5, pady=2)

        ttk.Entry(self.master, textvariable=self.chamber_number).grid(row=1, column=1, padx=5, pady=2)
        ttk.Entry(self.master, textvariable=self.animal_tag).grid(row=2, column=1, padx=5, pady=2)

        self.folder_entry = ttk.Entry(self.master, textvariable=self.save_path, state='readonly')
        self.folder_entry.grid(row=3, column=1, padx=5, pady=2)
        ttk.Button(self.master, text='Select Folder', command=self.select_folder).grid(row=3, column=2, padx=5, pady=2)

        self.stim_set_entry = ttk.Entry(self.master, textvariable=self.stim_set)
        self.stim_set_entry.grid(row=4, column=1, columnspan=2, sticky='ew', padx=5, pady=2)

        # Alternating signal list (e.g. "xw"). Empty means no alternation.
        self.signal_entry = ttk.Entry(self.master, textvariable=self.signal_input)
        self.signal_entry.grid(row=5, column=1, sticky='ew', padx=5, pady=2)
        
        self.start_entry = ttk.Entry(self.master, textvariable=self.scheduled_start)
        self.start_entry.grid(row=6, column=1, sticky='w', padx=5, pady=2)

        self.record_button = ttk.Button(self.master, text='Record & Analyze', command=self.toggle_record)
        self.record_button.grid(row=7, column=0, sticky='ew', padx=5, pady=5)

        ttk.Button(self.master, text='Capture Image', command=self.capture_and_display).grid(row=7, column=2, sticky='ew', padx=5, pady=5)
        ttk.Button(self.master, text='Capture & Calibrate Plate', command=self.capture_and_calibrate).grid(row=8, column=2, sticky='ew', padx=5, pady=5)

        self.info_label = ttk.Label(self.master, text='')
        self.info_label.grid(row=8, column=0, columnspan=2, padx=5, pady=5)

        self.image_label = ttk.Label(self.master)
        self.image_label.grid(row=9, column=0, columnspan=3, sticky='nsew', padx=5, pady=5)

        ttk.Checkbutton(self.master, text="Manual", variable=self.manual_cal, command=self.toggle_coords).grid(row=7, column=3, padx=5, pady=2)

        self.coord_entries.append(ttk.Entry(self.master, textvariable=self.tl_x))
        self.coord_entries.append(ttk.Entry(self.master, textvariable=self.tl_y))
        self.coord_entries.append(ttk.Entry(self.master, textvariable=self.tr_x))
        self.coord_entries.append(ttk.Entry(self.master, textvariable=self.tr_y))
        self.coord_entries.append(ttk.Entry(self.master, textvariable=self.bl_x))
        self.coord_entries.append(ttk.Entry(self.master, textvariable=self.bl_y))
        self.coord_entries.append(ttk.Entry(self.master, textvariable=self.br_x))
        self.coord_entries.append(ttk.Entry(self.master, textvariable=self.br_y))

        self.stim_colors = {
            'w': (255, 255, 255),
            'v': (211, 0, 148),
            'x': (200, 200, 200),
            'r': (0, 0, 255),
            'g': (0, 255, 0),
            's': (255, 165, 0),
            'h': (0, 69, 255)
        }

    def toggle_coords(self):
        """
        B: Shows or hides the eight manual corner coordinate entries. When
           Manual is checked, they are gridded in two rows of four below the
           main controls. When unchecked, they are removed from the grid and
           the existing self.plate_corners remain active.
        E: None.
        R: None.
        P: None. Reads self.manual_cal and self.coord_entries.
        """
        for i, entry in enumerate(self.coord_entries):
            if self.manual_cal.get():
                entry.grid(row=int(i / 4 - 1 + 10), column=int(i % 4), padx=5, pady=2)
            else:
                entry.grid_remove()

    def apply_warp(self, img):
        """
        B: Applies a perspective transform so the calibrated plate quad fills
           the target rectangle. Maps self.plate_corners (ordered TL, TR, BR,
           BL) onto (0, 0), (target_width, 0), (target_width, target_height),
           and (0, target_height). Pixels outside the quad are cropped out.
        E: cv2.error if self.plate_corners do not contain four finite,
           non-collinear points.
        R: numpy.ndarray: Warped image of shape (target_height, target_width)
           with the same channel count as img.
        P: img (numpy.ndarray): BGR or grayscale frame containing
           self.plate_corners.
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
        B: Renders a semi-transparent heads-up display (HUD) overlay onto the
           top-left of a video frame, displaying filled colored circles for
           active LED/solenoid signals (present in self.signal_tracker) and
           hollow gray rings for inactive signals.
        E: None (assumes frame is a valid 3-channel BGR OpenCV image).
        R: numpy.ndarray: The modified BGR frame with the status HUD rendered
           in-place.
        P: frame (numpy.ndarray): BGR video frame to annotate.
        """
        overlay = frame.copy()
        cv2.rectangle(overlay, (10, 10), (320, 50 + len(self.stim_colors) * 25), (30, 30, 30), -1)
        cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)

        cv2.putText(frame, "STIMULUS & SOLENOID STATUS", (20, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)

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
            circle_color = color if is_active else (80, 80, 80)
            thickness = -1 if is_active else 1
            cv2.circle(frame, (35, y_offset - 4), 8, circle_color, thickness)
            cv2.circle(frame, (35, y_offset - 4), 8, (200, 200, 200), 1)

            text_color = (255, 255, 255) if is_active else (150, 150, 150)
            cv2.putText(frame, label_text, (55, y_offset),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, text_color, 1, cv2.LINE_AA)
            y_offset += 25

        return frame

    def build_stim_dic(self):
        """
        B: Parses the Stimulus Set field into a dict mapping {time_key:
           command_characters}. If empty, loads a built-in schedule: Extended
           mode keys by clock time (HH.MM), while Short mode keys by elapsed
           seconds ({1: "w", 2: "wr", 3: "rg", 4: "gv", 5: "vs", 6: "sh",
           7: "h"}). Non-empty inputs are parsed as comma-separated
           "time:chars" pairs; duplicate timestamps concatenate their character
           strings.
        E: ValueError or IndexError (from missing colons or non-numeric times)
           is caught, displayed in an error dialog (posted to the Tk thread),
           and not re-raised; returns whatever dictionary entries were parsed
           prior to the error.
        R: dict[float, str]: Mapping of timestamp keys to ASCII command
           character strings.
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
                    {hour: "w", 23: "w", 9: "w", 23: "w"}
                    if self.extended_assay.get()
                    else {1: "w", 2: "wr", 3: "rg", 4: "gv", 5: "vs", 6: "sh", 7: "h"}
                )
        except Exception as e:
            self.master.after(0, lambda m=str(e): messagebox.showerror("Error", f"Stimulus set input error: {m}"))
        return stim_dic

    def switch_signal(self, duration, last_time):
        """
        B: Alternates active signals in self.signal_list whenever `duration`
           seconds have elapsed since `last_time`. Prints the ASCII-encoded
           signal toggled on and off to stdout, updates self.signal_tracker,
           and rotates self.signal_list.
        E: None.
        R: float: Updated perf_counter timestamp if the switch occurred,
           otherwise the unchanged `last_time`.
        P: duration (float): Minimum seconds between signal toggles.
           last_time (float): Timestamp (from time.perf_counter()) of the last
           toggle.
        """
        if time.perf_counter() - last_time >= duration:
            last_time = time.perf_counter()
            print(self.signal_list[0].encode('ascii'))
            self.signal_tracker.add(self.signal_list[0])
            print(self.signal_list[len(self.signal_list) - 1].encode('ascii'))
            self.signal_tracker.remove(self.signal_list[len(self.signal_list) - 1])
            self.signal_list = self.signal_list[1:] + self.signal_list[0]
        return last_time

    def send_serial(self):
        """
        B: Simulates sending stimulus ASCII characters to an Arduino on a
           daemon thread while recording is active. Waits 2 seconds for
           simulated board reset, then clears self.signal_tracker and loads
           self.signal_list from the Signal Input field (empty means no
           alternation). Polls at 0.05 s intervals, and matches either clock
           time (HH.MM in Extended mode) or quantized elapsed seconds (Short
           mode) against build_stim_dic(). Prints each stimulus character and
           its encoded ASCII byte to stdout and updates self.signal_tracker for
           the live HUD overlay. Clears the tracker and prints the shutdown
           character ('c') once recording finishes.
        E: Exceptions during initialization are caught and shown in an error
           dialog (posted to the Tk thread, not re-raised). Unhandled errors
           inside the loop propagate out of the daemon thread.
        R: None.
        P: None. Reads self.recording, self.extended_assay, self.stim_set,
           self.signal_input, and self.shutdown.
        """
        try:
            time.sleep(2)
        except Exception as e:
            self.master.after(0, lambda m=str(e): messagebox.showerror("Error", f"Could not connect to Arduino: {m}"))
            return

        self.signal_tracker.clear()
        self.signal_list = self.signal_input.get().split(":")[0] if self.signal_input.get() else ""
        self.signal_delay = float(self.signal_input.get().split(":")[1]) if self.signal_input.get() else 0
        
        round_time = -1
        delay = 0.05
        arduino_start = time.perf_counter()
        stim_dic = self.build_stim_dic()
        last_time = time.perf_counter()
        recording_tracker = 0

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
                        print(serial_char.encode('ascii'))
                        if serial_char in self.signal_tracker:
                            self.signal_tracker.remove(serial_char)
                        else:
                            self.signal_tracker.add(serial_char)

            if len(set(self.signal_list) & self.signal_tracker) == 1:
                last_time = self.switch_signal(self.signal_delay, last_time)
            else:
                for off_char in self.signal_list:
                    if off_char in self.signal_tracker:
                        print(off_char.encode('ascii'))
                        self.signal_tracker.remove(off_char)

            if self.recording and recording_tracker == 0:
                recording_tracker = 1
            elif (not self.recording) and recording_tracker == 1:
                recording_tracker = 2

            time.sleep(delay)

        recording_tracker = 0
        self.signal_tracker.clear()
        print(self.shutdown.encode('ascii'))

    def run_arduino(self):
        """
        B: Spawns and starts self.send_serial on a daemon thread so simulated
           serial timing runs concurrently with video capture and terminates if
           the main window closes.
        E: Exception if thread creation/start fails is caught, shown in an
           error dialog (posted to the Tk thread), and not re-raised.
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
        B: Opens a native directory picker dialog and writes the selected
           directory path into self.save_path. Cancelling leaves the existing
           path unchanged.
        E: None.
        R: None.
        P: None.
        """
        folder = filedialog.askdirectory()
        if folder:
            self.save_path.set(folder)

    def get_filename(self):
        """
        B: Constructs the session filename prefix combining chamber number,
           animal tag, and current local timestamp (YYYY-MM-DD_HH:MM:SS) with a
           leading slash.
        E: None.
        R: str: Path suffix formatted as
           "/Chamber<num>_Tag<tag>_<YYYY-MM-DD_HH:MM:SS>".
        P: None. Reads self.chamber_number and self.animal_tag.
        """
        now = datetime.datetime.now()
        return f"/Chamber{self.chamber_number.get()}_Tag{self.animal_tag.get()}_{now.strftime('%Y-%m-%d_%H:%M:%S')}"

    def toggle_record(self):
        """
        B: Starts a new recording session or signals an active one to stop.
           Validates that the webcam is free, a save folder is selected, and
           duration is a valid integer before setting self.recording = True,
           starting record_with_preview on a daemon thread, and relabeling the
           button to "Stop". Clicking while recording sets self.recording =
           False.
        E: A busy webcam, missing folder, or ValueError from a non-integer
           duration is caught, displayed in an error dialog, and not
           re-raised.
        R: None.
        P: None. Reads self.recording, self.busy, self.save_path, and
           self.record_duration.
        """
        if not self.recording:
            if self.busy:
                messagebox.showerror("Error", "The camera is busy with another task.")
                return
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
        B: Captures video from the default webcam (index 0) at 1920x1080 and
           writes it to an MP4 file at 2 fps (Extended) or 30 fps (Short);
           frames the webcam returns at another size are resized. Starts the
           simulated Arduino thread, optionally waits in a live preview loop
           until Scheduled Start Time (HH.MM, compared rounded to 2 places),
           records for the requested duration (timed from the scheduled
           start) while displaying a live OpenCV preview window with the HUD
           status overlay, then releases the webcam and clears self.busy.
           Clearing self.recording also ends send_serial. process_video()
           runs afterwards only if self.processing_on is True. Widget updates
           are posted to the Tk thread with master.after.
        E: Failures are caught and shown in an error dialog; the webcam, busy
           flag, recording flag, and button are always reset.
        R: None. Writes "<stem>.mp4"; with processing on, also
           "<stem>_motion_indices.csv" and "<stem>.avi".
        P: None. Reads self.save_path, self.extended_assay,
           self.scheduled_start, self.record_duration, and self.processing_on.
        """
        save_dir = self.save_path.get()
        fileType = ".mp4"
        self.frame_rate = 2 if self.extended_assay.get() else 30
        rec_w, rec_h = 1920, 1080
        self.busy = True
        filename = None
        mp4_path = None
        completed = False
        cap = None
        out = None

        try:
            scheduled = self.scheduled_start.get()
            scheduled_val = float(scheduled) if scheduled != "" else None
            duration = int(self.record_duration.get())

            cap = cv2.VideoCapture(0)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, rec_w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, rec_h)
            cv2.namedWindow("Camera Preview", cv2.WINDOW_NORMAL)

            self.run_arduino()
            time.sleep(2)

            while self.recording and scheduled_val is not None:
                now = datetime.datetime.now()
                current_hhmm = round(now.hour + (now.minute / 100.0), 2)
                if current_hhmm == scheduled_val:
                    break
                ret, frame = cap.read()
                if ret:
                    cv2.imshow("Camera Preview", frame)
                    cv2.waitKey(1)
                time.sleep(0.05)

            filename = save_dir + self.get_filename()
            mp4_path = f"{filename}{fileType}"

            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            out = cv2.VideoWriter(mp4_path, fourcc, self.frame_rate, (rec_w, rec_h))

            self.master.after(0, lambda: self.info_label.config(text=f"Recording to {filename}"))

            start_time = time.monotonic()
            while self.recording and (time.monotonic() - start_time < duration):
                ret, frame = cap.read()
                if not ret:
                    break

                if frame.shape[1] != rec_w or frame.shape[0] != rec_h:
                    frame = cv2.resize(frame, (rec_w, rec_h))

                display_frame = self.draw_status_overlay(frame.copy())
                out.write(frame)

                cv2.imshow("Camera Preview", display_frame)
                cv2.waitKey(1)

            completed = self.recording          # False if Stop was pressed
            # Clearing this also ends send_serial.
            self.recording = False

        except Exception as e:
            self.master.after(0, lambda m=str(e): messagebox.showerror("Error", f"Recording failed: {m}"))
        finally:
            self.recording = False
            if out is not None:
                out.release()
            if cap is not None:
                cap.release()
            cv2.destroyAllWindows()
            self.busy = False
            self.master.after(0, lambda: self.record_button.config(text='Record & Analyze'))

        if mp4_path is None:
            return

        msg = f"Video saved to {filename}" if completed else "Recording stopped."
        self.master.after(0, lambda: self.info_label.config(text=msg))

        if os.path.exists(mp4_path) and self.processing_on:
            self.process_video(mp4_path, f"{filename}_motion_indices.csv")

    def find_plate_corners(self, img):
        """
        B: Sets self.plate_corners and returns img with detection contours
           drawn. In Manual mode, reads the eight coordinate entries directly
           without image processing. In Automatic mode, converts to grayscale,
           applies Gaussian blur, contrast stretching, Otsu-scaled Canny edge
           detection, morphological closing, and contour approximation. Draws
           convex hulls (red), polygons (green), and contours (blue), ordering
           and saving the first 4-vertex polygon found.
        E: cv2.error if img is not a valid BGR image on the automatic path. A
           missed plate does not raise; prints "Failed to find corners" and
           keeps prior corners.
        R: numpy.ndarray: Annotated img in Automatic mode, or unmodified img in
           Manual mode.
        P: img (numpy.ndarray): BGR frame; modified in-place during automatic
           detection.
        """
        if self.manual_cal.get():
            self.plate_corners = np.float32([
                [self.tl_x.get(), self.tl_y.get()],
                [self.tr_x.get(), self.tr_y.get()],
                [self.br_x.get(), self.br_y.get()],
                [self.bl_x.get(), self.bl_y.get()]
            ])
            return img

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (15, 15), 0)
        blurred = cv2.convertScaleAbs(blurred, alpha=1.5, beta=0)

        otsu_thresh, _ = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        edged1 = cv2.Canny(blurred, int(otsu_thresh / 14), int(otsu_thresh / 5.5))
        contours1, _ = cv2.findContours(edged1.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        cv2.drawContours(edged1, contours1, -1, (255, 255, 255), 20)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
        closed_mask = cv2.morphologyEx(edged1, cv2.MORPH_CLOSE, kernel)

        edged2 = cv2.Canny(closed_mask, 20, 50)
        contours2, _ = cv2.findContours(edged2.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours2, key=cv2.contourArea, reverse=True)

        for c in contours:
            convex = cv2.convexHull(c)
            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(convex, 0.02 * peri, True)

            cv2.drawContours(img, [convex], -1, (0, 0, 255), 4)
            cv2.drawContours(img, [approx], -1, (0, 255, 0), 4)
            cv2.drawContours(img, contours, -1, (255, 0, 0), 4)

            if len(approx) == 4:
                pts = approx.reshape(4, 2).astype(np.float32)
                print("Successfully found plate corners.")
                self.plate_corners = self.order_points(pts)
                print(self.plate_corners)
                return img

        print("Failed to find corners")
        return img

    def order_points(self, pts):
        """
        B: Orders four 2D corner coordinates into [top-left, top-right,
           bottom-right, bottom-left] using coordinate sums (x+y) and
           differences (y-x) so they match the destination order expected by
           cv2.getPerspectiveTransform().
        E: IndexError or ValueError if pts does not have shape (4, 2).
        R: numpy.ndarray: float32 array of shape (4, 2) ordered [TL, TR, BR, BL].
        P: pts (numpy.ndarray): Four (x, y) corner coordinates of shape (4, 2).
        """
        rect = np.zeros((4, 1, 2), dtype="float32")
        s = pts.sum(axis=1)
        rect[0] = pts[np.argmin(s)]
        rect[2] = pts[np.argmax(s)]

        diff = np.diff(pts, axis=1)
        rect[1] = pts[np.argmin(diff)]
        rect[3] = pts[np.argmax(diff)]

        return rect.reshape(4, 2)

    def process_video(self, video_path, output_csv_path):
        """
        B: Computes frame-by-frame motion indices across a 96-well grid (rows
           A-H, cols 1-12) and writes both a CSV table and a binary motion-mask
           AVI video. Warps each grayscale frame, computes absolute pixel
           difference against the previous frame, thresholds at 25, and counts
           nonzero pixels inside each inset well bounding box. Only called when
           self.processing_on is True.
        E: Unreadable video stream or empty first frame prints an error /
           releases capture and returns early without raising. File I/O or
           VideoWriter errors propagate.
        R: None. Writes `output_csv_path` and a thresholded ".avi" file beside
           `video_path`.
        P: video_path (str): Path to the recorded MP4 file.
           output_csv_path (str): Destination path for the well motion-index CSV.
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

        row_step = (self.target_height - 2 * self.pmarg_y) / 8.0
        col_step = (self.target_width - 2 * self.pmarg_x) / 12.0
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
                header.append(f"{chr(65 + r)}{c + 1}")
            writer.writerow(header)

            fourcc = cv2.VideoWriter_fourcc(*'XVID')
            analysis_path = video_path.replace(".mp4", ".avi")
            analysis_writer = cv2.VideoWriter(analysis_path, fourcc, self.frame_rate, (self.target_width, self.target_height))

            frame_idx = 0
            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break
                frame_idx += 1
                current_time = frame_idx / fps

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                warp_frame = self.apply_warp(gray)

                diff = cv2.absdiff(warp_frame, prev_warp)
                _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
                thresh_color = cv2.cvtColor(thresh, cv2.COLOR_GRAY2BGR)
                analysis_writer.write(thresh_color)

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
        """
        B: Captures a single frame from the webcam at 1296x972, flushing 5
           frames for auto-exposure settling, resizes it to 1296x972 if the
           webcam returned another size (no zoom crop), draws a 50-pixel
           coordinate grid if Manual mode is enabled, saves 'image.jpg' to
           self.save_path, and displays a 640x400 preview in a Toplevel popup
           window. Runs on the Tk thread.
        E: A busy webcam, RuntimeError (failed frame grab), OpenCV, PIL, or
           filesystem errors are caught, shown in an error dialog, and not
           re-raised.
        R: None. Writes "<save_path>/image.jpg".
        P: None. Reads self.busy, self.save_path, and self.manual_cal.
        """
        if self.busy:
            messagebox.showerror("Error", "The camera is busy with another task.")
            return
        try:
            image_path = os.path.join(self.save_path.get(), 'image.jpg')
            width, height = 1296, 972

            cap = cv2.VideoCapture(0)
            try:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

                for _ in range(5):
                    ret, frame = cap.read()
                    if not ret or frame is None:
                        raise RuntimeError("Failed to grab frame from camera.")
            finally:
                cap.release()

            grid_img = cv2.resize(frame, (width, height), interpolation=cv2.INTER_LINEAR)

            if self.manual_cal.get():
                step_size = 50
                for y_idx in range(6):
                    color = (0, 255 - 30 * y_idx, 30 * y_idx)
                    cv2.line(grid_img, (0, y_idx * step_size), (width, y_idx * step_size), color, 4)
                    cv2.line(grid_img, (0, height - y_idx * step_size), (width, height - y_idx * step_size), color, 4)
                for x_idx in range(6):
                    color = (0, 255 - 40 * x_idx, 40 * x_idx)
                    cv2.line(grid_img, (x_idx * step_size, 0), (x_idx * step_size, height), color, 4)
                    cv2.line(grid_img, (width - x_idx * step_size, 0), (width - x_idx * step_size, height), color, 4)

            cv2.imwrite(image_path, grid_img)

            rgb_img = cv2.cvtColor(grid_img, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb_img).resize((640, 400), Image.LANCZOS)

            popup = tk.Toplevel(self.master)
            popup.title("Captured Image" + (" (Grid Shown)" if self.manual_cal.get() else " (No Grid)"))

            tk_img = ImageTk.PhotoImage(pil_img)
            popup_label = ttk.Label(popup, image=tk_img)
            popup_label.image = tk_img
            popup_label.pack(padx=10, pady=10)

        except Exception as e:
            messagebox.showerror("Error", f"Could not capture image: {e}")

    def capture_and_calibrate(self):
        """
        B: Button callback. Checks that the webcam is free and a folder is
           chosen, then starts calibrate_plate on a daemon thread, the same
           way toggle_record starts record_with_preview. The Tk thread is
           never blocked.
        E: A busy webcam or a missing folder is shown in an error dialog and
           nothing is started.
        R: None.
        P: None. Reads self.busy and self.save_path.
        """
        if self.busy:
            messagebox.showerror("Error", "The camera is busy with another task.")
            return
        if not self.save_path.get():
            messagebox.showerror("Error", "Please select a folder path first.")
            return
        self.calibrate_thread = threading.Thread(target=self.calibrate_plate, daemon=True)
        self.calibrate_thread.start()

    def calibrate_plate(self):
        """
        B: Opens a live 1920x1080 webcam preview window until the user presses
           SPACE or ENTER to grab a calibration frame. Runs
           find_plate_corners() and apply_warp(), overlays the 8x12 well grid
           (orange outer borders, green inner dividers), deletes the temporary
           JPEG files, and shows a 640x400 Toplevel preview. The popup and
           status text are created on the Tk thread via master.after. Runs on
           a daemon thread.
        E: Capture, image load, or display errors are caught, shown in an
           error dialog (posted to the Tk thread), and not re-raised. The
           webcam and busy flag are always reset.
        R: None. Updates self.plate_corners on success.
        P: None. Reads self.save_path and plate geometry attributes.
        """
        image_path = os.path.join(self.save_path.get(), 'calibration_capture.jpg')
        preview_path = os.path.join(self.save_path.get(), 'calibrated_plate_preview.jpg')
        window_name = "Calibration Preview - Press SPACE or ENTER to capture"
        self.busy = True
        try:
            cap = cv2.VideoCapture(0)
            frame = None
            try:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
                cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

                while True:
                    ret, current_frame = cap.read()
                    if ret:
                        frame = current_frame
                        cv2.imshow(window_name, frame)

                    key = cv2.waitKey(1) & 0xFF
                    if key in (13, 32):
                        break
            finally:
                cap.release()
                cv2.destroyAllWindows()

            if frame is None:
                self.master.after(0, lambda: messagebox.showerror("Error", "Failed to capture calibration image."))
                return

            if frame.shape[1] != 1920 or frame.shape[0] != 1080:
                frame = cv2.resize(frame, (1920, 1080))
            cv2.imwrite(image_path, frame)

            img = cv2.imread(image_path)
            if img is None:
                self.master.after(0, lambda: messagebox.showerror("Error", "Failed to load calibration image."))
                return

            gray = self.find_plate_corners(img)
            warped = self.apply_warp(gray)

            row_step = (self.target_height - 2 * self.pmarg_y) / 8.0
            col_step = (self.target_width - 2 * self.pmarg_x) / 12.0
            for r in range(9):
                y = int(r * row_step) + self.pmarg_y
                cv2.line(warped, (0, y), (self.target_width, y), (0, 150, 255 if r % 8 == 0 else 0), 2)
            for c in range(13):
                x = int(c * col_step) + self.pmarg_x
                cv2.line(warped, (x, 0), (x, self.target_height), (0, 150, 255 if c % 12 == 0 else 0), 2)

            cv2.imwrite(preview_path, warped)
            pil_img = Image.open(preview_path).resize((640, 400), Image.LANCZOS)

            for path in (image_path, preview_path):
                if os.path.exists(path):
                    os.remove(path)
                else:
                    print(f"Failed to delete {path}")

            # Widgets must be created on the Tk thread.
            def show_popup():
                popup = tk.Toplevel(self.master)
                popup.title("Plate Calibration Preview")
                tk_img = ImageTk.PhotoImage(pil_img)
                popup_label = ttk.Label(popup, image=tk_img)
                popup_label.image = tk_img
                popup_label.pack(padx=10, pady=10)
                self.info_label.config(text="Calibration preview generated with grid overlay.")
            self.master.after(0, show_popup)

        except Exception as e:
            self.master.after(0, lambda m=str(e): messagebox.showerror("Error", f"Calibration capture failed: {m}"))
        finally:
            self.busy = False


def main():
    """
    B: Initializes the Tk root window, instantiates EZMiceApp, and enters the
       Tkinter event mainloop until the operator closes the window.
    E: Tk initialization or runtime errors propagate.
    R: None.
    P: None.
    """
    root = tk.Tk()
    app = EZMiceApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
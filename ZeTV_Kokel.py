#!/usr/bin/env python3
"""
EZMice v0.4.1 — well-plate recording and per-well motion scoring.

Behavior: Records a plate assay, pulses an Arduino from a stimulus schedule, and
   writes a motion index for each of the 96 wells. Tkinter front end for a
   Raspberry Pi: PiCamera records H264, ffmpeg remuxes it to MP4, and OpenCV
   warps the plate into a fixed rectangle before frame-subtraction.
   Calibration stores the plate corners and the white-balance gains that
   later recordings reuse.
Exceptions: None at import. Each method reports its own failures.
Returns: None. Runs until the window is closed.
Parameters: None. Start with `python3 ezmice.py`.
"""

from asyncio import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk
import threading
import datetime
import picamera
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
       Holds the form fields, the Pi camera settings, the Arduino thread, and
       the plate geometry. Button callbacks start work; serial output and
       recording run on daemon threads.
    E: None from construction. See each method for the errors it reports.
    R: None. Keep the instance alive for as long as master.mainloop() runs.
    P: master — the Tk root this UI is gridded onto.
    """

    def __init__(self, master):
        """
        B: Stores blank form fields and default plate geometry, then builds the
           window. Recording and stimulus values start empty. Plate corners
           default to a rectangle inside a 1296x972 frame; the warp target is
           600x400. pmarg_x and pmarg_y are pixel margins on that canvas.
           awbgains is replaced when Capture & Calibrate locks white balance.
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
        # Assay type: "short" for 30 fps, "extended" for 2 fps. The latter is used in overnight
        self.extended_assay = True # "short" or "extended"
        # Stimulus schedule text ("time:chars,...") and the serial tick length.
        self.stim_set = tk.StringVar()
        self.delay = tk.StringVar()

        # Source-image corners (TL, TR, BR, BL) and the rectangle they warp into.
        self.plate_corners = np.float32([[100, 100], [1196, 100], [1196, 872], [100, 872]])
        self.target_width = 600
        self.target_height = 400
        # Pixel margin on the warped canvas. Scoring and the calibration grid
        # each apply it with their own formula; see those methods.
        self.pmarg_x = 50
        self.pmarg_y = 40

        # Red and blue gains. Calibration overwrites this after sampling auto AWB.
        self.awbgains = (1.0, 2.3)
        # Shutter speed. Calibration overwrites this after sampling auto exposure.
        self.shutter = 100000
        # Frame rate: 30 fps standard for short assays, 2 fps for extended studies
        self.frame_rate = 2 if self.extended_assay else 30

        self.create_widgets()

    def create_widgets(self):
        """
        B: Lays out the form, the three action buttons, and the status line.
           Column 0 is labels, column 1 is entries, column 2 is the folder and
           capture buttons. The folder entry is read-only; Select Folder is the
           only writer. image_label is left empty; capture paths use a popup.
        E: None.
        R: None. Widgets that later methods update are stored on self.
        P: None. Uses self.master and the StringVars created in __init__.
        """
        ttk.Label(self.master, text='Record Duration (s):').grid(row=0, column=0, sticky='w', padx=5, pady=2)
        ttk.Label(self.master, text='Chamber Number:').grid(row=1, column=0, sticky='w', padx=5, pady=2)
        ttk.Label(self.master, text='Animal Tag:').grid(row=2, column=0, sticky='w', padx=5, pady=2)
        ttk.Label(self.master, text='Folder Path:').grid(row=3, column=0, sticky='w', padx=5, pady=2)
        ttk.Label(self.master, text='Stimulus Set:').grid(row=4, column=0, sticky='w', padx=5, pady=2)
        ttk.Label(self.master, text='Loop Delay (s):').grid(row=5, column=0, sticky='w', padx=5, pady=2)

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
        self.delay_entry = ttk.Entry(self.master, textvariable=self.delay)
        self.delay_entry.grid(row=5, column=1, sticky='w', padx=5, pady=2)

        self.record_button = ttk.Button(self.master, text='Record & Analyze', command=self.toggle_record)
        self.record_button.grid(row=6, column=0, sticky='ew', padx=5, pady=5)

        ttk.Button(self.master, text='Capture Image', command=self.capture_and_display).grid(row=6, column=2, sticky='ew', padx=5, pady=5)
        ttk.Button(self.master, text='Capture & Calibrate Plate', command=self.capture_and_calibrate).grid(row=7, column=2, sticky='ew', padx=5, pady=5)

        self.info_label = ttk.Label(self.master, text='')
        self.info_label.grid(row=8, column=0, columnspan=3, padx=5, pady=5)

        self.image_label = ttk.Label(self.master)
        self.image_label.grid(row=9, column=0, columnspan=3, sticky='nsew', padx=5, pady=5)

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

    def build_stim_dic(self):
        """
        B: Turns the Stimulus Set field into {time_seconds: command_characters}.
           An empty field selects {1: "w", 2: "wr", 3: "rg", 4: "gv", 5: "vs",
           6: "sh", 7: "h"}. Otherwise the field is comma-separated items of
           "time:chars". Characters for the same time are concatenated; each
           is later written as one ASCII byte.
        E: A missing colon or a non-numeric time is caught, shown in an error
           dialog, and not re-raised. The dict parsed before that item is returned.
        R: Dict mapping a time in seconds to the serial characters due then.
        P: None. Reads self.stim_set.
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
            elif self.extended_assay:
                stim_dic = {datetime.datetime.now().hour: "w", 23: "w", 9: "w", 23: "w"}
            else:
                stim_dic = {1: "w", 2: "wr", 3: "rg", 4: "gv", 5: "vs", 6: "sh", 7: "h"}

        except Exception as e:
            messagebox.showerror("Error", f"Stimulus set input error: {e}")
        return stim_dic

    def send_serial(self):
        """
        B: Writes stimulus characters to the Arduino while a recording runs.
           Opens /dev/ttyACM0 at 9600 baud and waits 2 seconds for the board to
           reset. Time is quantized to multiples of Loop Delay (0.05 s if the
           field is empty). On each new step whose time is a schedule key, every
           character of that value is written. The loop ends when self.recording
           becomes false, then the port is closed. Runs on a daemon thread.
        E: A failure to open the port is caught, shown in an error dialog, and
           not re-raised. A non-numeric Loop Delay raises ValueError from float()
           and is not caught. Write errors on the open port propagate.
        R: None.
        P: None. Reads self.delay, self.recording, and the stimulus field.
        """
        try:
            arduino = serial.Serial('/dev/ttyACM0', 9600, timeout=1)
            time.sleep(2)
        except Exception as e:
            messagebox.showerror("Error", f"Could not connect to Arduino: {e}")
            return

        round_time = -1
        arduino_start = time.perf_counter()
        delay = float(self.delay.get()) if self.delay.get() != "" else 0.05
        stim_dic = self.build_stim_dic()

        # Fire once per quantized timestamp, not once per loop iteration.
        while self.recording:
            if self.extended_assay:
                current_round_time = datetime.datetime.now().hour
            else:
                current_round_time = int((time.perf_counter() - arduino_start) / delay) * delay
            if round_time != current_round_time:
                round_time = current_round_time
                if round_time in stim_dic:
                    for serial_char in stim_dic[round_time]:
                        arduino.write(serial_char.encode('ascii'))
            time.sleep(delay)
        arduino.close()

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
        return f"/Chamber{self.chamber_number.get()}_Tag{self.animal_tag.get()}_{now.strftime('%Y%m%d_%H%M%S')}"

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
        """
        B: Records H264 for the requested duration, remuxes to MP4, then scores
           motion. Camera is 1296x972, backlight exposure, ISO 100, zoomed to
           the plate, with white balance forced to self.awbgains. The Arduino
           thread starts with the recording. The loop waits one second at a
           time until Stop is pressed or the duration elapses. ffmpeg copies
           the H264 into an MP4 at self.frame_rate fps. process_video runs only when that
           MP4 exists. Status text is posted to the Tk thread with master.after.
           Clearing self.recording also ends send_serial before the remux.
        E: Camera, filesystem, and ffmpeg failures are not caught here and
           propagate out of the daemon thread.
        R: None. Writes "<stem>.h264", "<stem>.mp4", and the analysis outputs.
        P: None. Reads the form fields and self.awbgains.
        """
        save_dir = self.save_path.get()
        filename = save_dir + self.get_filename()
        fileType = ".mp4"
        #os.chdir(save_dir)

        with picamera.PiCamera() as camera:
            camera.resolution = (1296, 972)
            camera.framerate = self.frame_rate
            camera.exposure_mode = 'backlight'
            camera.awb_gains = self.awbgains
            camera.awb_mode = 'off'
            camera.iso = 100
            camera.zoom = (0.4, 0.3, 0.25, 0.25)

            camera.start_preview()
            self.run_arduino()
            camera.start_recording("{0}.h264".format(filename))
            self.master.after(0, lambda: self.info_label.config(text=f"Recording to {filename}"))
            elapsed = 0
            while self.recording and elapsed < int(self.record_duration.get()):
                camera.wait_recording(1)
                elapsed += 1

            camera.stop_recording()
            camera.stop_preview()

            if self.recording:
                self.master.after(0, lambda: self.info_label.config(text=f"Video saved to {filename}"))
            else:
                self.info_label.config(text="Recording stopped.")

            # Clearing this also ends send_serial before the remux and analysis.
            self.recording = False
            camera.close()
            self.record_button.config(text='Record & Analyze')

            # Stream copy: keep the camera's frames, just change the container.
            #command = "MP4Box -add {0}.h264 {1}.mp4".format(filename, filename)
            command = f"ffmpeg -y -framerate {self.frame_rate} -i {filename}.h264 -c copy {filename}.mp4"
            call([command], shell=True)

            if os.path.exists(filename + fileType):
                self.process_video(filename + fileType, f"{filename}_motion_indices.csv")

    def find_plate_corners(self, img):
        """
        B: Finds a four-corner plate outline and stores it on self.plate_corners.
           The grayscale frame is blurred and contrast-stretched. Canny runs
           with thresholds at 1/12 and 1/5 of that image's Otsu value. Those
           contours are painted back on, 20 px thick, and a 7x7 close joins
           gaps. A second Canny (20, 50) produces the contours that are scored.
           Largest first: the convex hull is drawn red, its polygon green, and
           every contour blue. The first polygon with 4 vertices is ordered
           and saved. If none qualifies, the existing corners are left in place
           and the overlay is still returned.
        E: OpenCV raises if img is not a BGR image. A missed plate is not an
           exception; the method prints "Failed to find corners" and returns img.
        R: img, with the detection overlay drawn on it.
        P: img — BGR frame, modified in place.
        """
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (15,15), 0) ###gray
        # alpha > 1 stretches contrast so the plate rim separates from the bench.
        blurred = cv2.convertScaleAbs(blurred, alpha=1.5, beta=0) #blurred

        # Otsu supplies the Canny thresholds; the binary image itself is discarded.
        otsu_thresh, _ = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        edged1 = cv2.Canny(blurred, int(otsu_thresh/12), int(otsu_thresh/5))

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
           The first grayscale frame is warped and becomes the reference. Each
           later frame is warped, subtracted, and thresholded at 25. A well's
           motion index is the count of white pixels in its box. Well steps are
           (target + 2*margin) / 8 rows and / 12 columns, then shifted in by
           pmarg_x and pmarg_y. The CSV header is Frame, Time (s), then A1..H12.
           Time uses the file fps, or 30 if the file reports none. The mask
           video is XVID at 30 fps, target_width by target_height, named
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

        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps <= 0:
            fps = 30.0

        ret, prev_frame = cap.read()
        if not ret:
            cap.release()
            return

        prev_gray = cv2.cvtColor(prev_frame, cv2.COLOR_BGR2GRAY)
        #prev_gray = self.find_plate_corners(prev_frame)
        prev_warp = self.apply_warp(prev_gray)

        # 8 rows (A-H) by 12 columns. The step includes both margins, so the
        # outer wells sit partly past the warped image and those slices clip.
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
            fourcc = cv2.VideoWriter_fourcc(*'XVID')
            analysis_path = video_path.replace(".mp4", "_analysis.avi")
            analysis_writer = cv2.VideoWriter(analysis_path, fourcc, 30, (self.target_width,self.target_height))

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
        B: Grabs one still and opens it in the system image viewer. Uses a
           fresh PiCamera at 1920x1080 with the camera defaults, so awbgains
           and the plate zoom are left alone. The file is image.jpg in the
           current working directory.
        E: Capture and display failures are caught, shown in an error dialog,
           and not re-raised.
        R: None.
        P: None.
        """
        try:
            with picamera.PiCamera() as camera:
                camera.resolution = (1920, 1080)
                camera.capture('image.jpg')

            image = Image.open('image.jpg')
            image.show()
        except Exception as e:
            messagebox.showerror("Error", f"Could not capture image: {e}")

    def capture_and_calibrate(self):
        """
        B: Locks white balance, detects the plate, and previews the 8x12 grid.
           Auto white balance runs for 2 seconds, then awb_gains are frozen onto
           self for record_with_preview. After a 5 second preview it captures
           1296x972 with the plate zoom. Corner detection draws on that frame;
           the annotated frame is warped and a 9-by-13 line grid is drawn on
           top. Outer boundary lines are orange; inner lines are green. The
           preview is shown from the JPEG, then both temporary JPEGs are deleted.
           time.sleep(1) runs on the Tk thread, so the window paints after return.
        E: Capture, load, and preview failures are caught, shown in an error
           dialog, and not re-raised. A missing image file returns from its own
           dialog before the temporary files are deleted.
        R: None. On the success path, self.plate_corners and self.awbgains are updated.
        P: None. Reads self.save_path and the plate geometry fields.
        """
        try:
            image_path = self.save_path.get() + '/calibration_capture.jpg'
            preview_path = self.save_path.get()  + '/calibrated_plate_preview.jpg'

            with picamera.PiCamera() as camera:
                camera.resolution = (int(1296), int(972))
                camera.exposure_mode = 'backlight' #auto
                camera.awb_mode = 'auto'
                camera.iso = 100
                camera.zoom = (0.4, 0.3, 0.25, 0.25)

                # Let auto AWB and auto exposure settle, then pin the settings for this process.
                time.sleep(2)
                self.awbgains = camera.awb_gains
                self.shutter = camera.shutter_speed
                camera.awb_mode = 'off'
                camera.exposure_mode = 'off'
                camera.shutter_speed = self.shutter
                camera.awb_gains = self.awbgains
                print(self.awbgains)

                camera.start_preview()
                time.sleep(5)
                camera.stop_preview()
                camera.capture(image_path)


            img = cv2.imread(image_path)
            if img is None:
                messagebox.showerror("Error", "Failed to load calibration image.")
                return


            gray = self.find_plate_corners(img)
            warped = self.apply_warp(gray)

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

            cv2.imwrite(preview_path, warped) #####warped
            pil_img = Image.open(preview_path)
            #Image.open(preview_path).show()
            pil_img = Image.open(preview_path).resize((640,400), Image.LANCZOS)

            popup = tk.Toplevel(self.master)
            popup.title("Plate Calibration Preview")

            tk_img = ImageTk.PhotoImage(pil_img)

            popup_label = ttk.Label(popup, image=tk_img)
            popup_label.image = tk_img  # Keep reference to prevent garbage collection!
            popup_label.pack(padx=10, pady=10)

            #Delete temporary files after a short delay to ensure the image is displayed
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



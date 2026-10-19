
#!/usr/bin/env python3

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image
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
    def __init__(self, master):
        self.master = master
        self.master.title('EZMice v0.4.1')

        # Improve the style
        self.style = ttk.Style()
        self.style.theme_use('clam')

        # Initialize variables
        self.record_duration = tk.StringVar()
        self.chamber_number = tk.StringVar()
        self.animal_tag = tk.StringVar()
        self.save_path = tk.StringVar()
        self.recording = False

        # Arduino variables
        self.stim_set = tk.StringVar()
        self.delay = tk.StringVar()

        # Plate Calibration Points & Target Dimensions
        self.plate_corners = np.float32([[100, 100], [1196, 100], [1196, 872], [100, 872]])
        self.target_width = 1200
        self.target_height = 800

        # Build the GUI
        self.create_widgets()

    def create_widgets(self):
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

        # Stimulus Entry
        self.stim_set_entry = ttk.Entry(self.master, textvariable=self.stim_set)
        self.stim_set_entry.grid(row=4, column=1, columnspan=2, sticky='ew', padx=5, pady=2)

        # Loop delay entry
        self.delay_entry = ttk.Entry(self.master, textvariable=self.delay)
        self.delay_entry.grid(row=5, column=1, sticky='w', padx=5, pady=2)

        # Action Buttons
        self.record_button = ttk.Button(self.master, text='Record & Analyze', command=self.toggle_record)
        self.record_button.grid(row=6, column=0, sticky='ew', padx=5, pady=5)

        ttk.Button(self.master, text='Capture Image', command=self.capture_and_display).grid(row=6, column=2, sticky='ew', padx=5, pady=5)
        ttk.Button(self.master, text='Capture & Calibrate Plate', command=self.capture_and_calibrate).grid(row=7, column=2, sticky='ew', padx=5, pady=5)

        # Labels for feedback and preview
        self.info_label = ttk.Label(self.master, text='')
        self.info_label.grid(row=8, column=0, columnspan=3, padx=5, pady=5)

        self.image_label = ttk.Label(self.master)
        self.image_label.grid(row=9, column=0, columnspan=3, sticky='nsew', padx=5, pady=5)

    def apply_warp(self, img):
        """Decomposed reusable helper for perspective transformation."""
        dst_pts = np.float32([
            [0, 0],
            [self.target_width, 0],
            [self.target_width, self.target_height],
            [0, self.target_height]
        ])
        matrix = cv2.getPerspectiveTransform(self.plate_corners, dst_pts)
        return cv2.warpPerspective(img, matrix, (self.target_width, self.target_height))

    def build_stim_dic(self):
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
                stim_dic = {1: "w", 2: "wr", 3: "rg", 4: 'gv', 5: "vs", 6: "sh", 7: "h"}
        except Exception as e:
            messagebox.showerror("Error", f"Stimulus set input error: {e}")
        return stim_dic

    def send_serial(self):
        try:
            arduino = serial.Serial('/dev/ttyACM0', 9600, timeout=1)
            time.sleep(2)
        except Exception as e:
            messagebox.showerror("Error", f"Could not connect to Arduino: {e}")
            return

        arduino_start = time.perf_counter()
        round_time = -1

        delay = float(self.delay.get()) if self.delay.get() != "" else 0.05
        stim_dic = self.build_stim_dic()

        while self.recording:
            current_round_time = int((time.perf_counter() - arduino_start) / delay) * delay
            if round_time != current_round_time:
                round_time = current_round_time
                if round_time in stim_dic:
                    for serial_char in stim_dic[round_time]:
                        arduino.write(serial_char.encode('ascii'))
            time.sleep(delay)

    def run_arduino(self):
        try:
            self.arduino_thread = threading.Thread(target=self.send_serial, daemon=True)
            self.arduino_thread.start()
        except Exception as e:
            messagebox.showerror("Error", f"Could not start Arduino thread: {e}")

    def select_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.save_path.set(folder)

    def get_filename(self):
        now = datetime.datetime.now()
        return f"/Chamber{self.chamber_number.get()}_Tag{self.animal_tag.get()}_{now.strftime('%Y%m%d_%H%M%S')}"

    def toggle_record(self):
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
        filename = save_dir + self.get_filename()
        fileType = ".mp4"
        os.chdir(save_dir)

        with picamera.PiCamera() as camera:
            camera.resolution = (1296, 972)
            camera.exposure_mode = 'auto'
            camera.awb_mode = 'auto'
            camera.iso = 100
            camera.zoom = (0.35, 0.3, 0.3, 0.2)

            camera.start_preview()
            self.run_arduino()
            camera.start_recording("{0}.h264".format(filename))
            self.info_label.config(text=f"Recording to {filename}")

            camera.wait_recording(int(self.record_duration.get()))

            camera.stop_recording()
            camera.stop_preview()

            if self.recording:
                self.info_label.config(text=f"Video saved to {filename}")
            else:
                self.info_label.config(text="Recording stopped.")

            self.recording = False
            self.record_button.config(text='Record & Analyze')

            command = "MP4Box -add {0}.h264 {1}.mp4".format(filename, filename)
            call([command], shell=True)

            if os.path.exists(filename + fileType):
                self.process_video(filename + fileType, f"{filename}_motion_indices.csv")

    def process_video(self, video_path, output_csv_path):
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
        prev_warp = self.apply_warp(prev_gray)

        # Define 96-well grid boundaries (12 columns x 8 rows)
        row_step = self.target_height / 8.0
        col_step = self.target_width / 12.0
        well_boxes = []
        for r in range(8):
            for c in range(12):
                x1 = int(c * col_step)
                y1 = int(r * row_step)
                x2 = int((c + 1) * col_step)
                y2 = int((r + 1) * row_step)
                well_boxes.append((r, c, x1, y1, x2, y2))

        with open(output_csv_path, mode='w', newline='') as csv_file:
            writer = csv.writer(csv_file)
            header = ['Frame', 'Time (s)']
            for r, c, _, _, _, _ in well_boxes:
                header.append(f"{chr(65+r)}{c+1}")
            writer.writerow(header)

            frame_idx = 0
            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break

                frame_idx += 1
                current_time = frame_idx / fps

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                warp_frame = self.apply_warp(gray)

                # Frame subtraction
                diff = cv2.absdiff(warp_frame, prev_warp)
                _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)

                row_data = [frame_idx, round(current_time, 3)]
                for r, c, x1, y1, x2, y2 in well_boxes:
                    well_roi = thresh[y1:y2, x1:x2]
                    motion_index = np.sum(well_roi > 0)
                    row_data.append(motion_index)

                writer.writerow(row_data)
                prev_warp = warp_frame

        cap.release()
        self.master.after(0, lambda: self.info_label.config(text=f"Analysis complete! Saved: {os.path.basename(output_csv_path)}"))

    def capture_and_display(self):
        try:
            with picamera.PiCamera() as camera:
                camera.resolution = (1920, 1080)
                camera.capture('image.jpg')

            image = Image.open('image.jpg')
            image.show()
        except Exception as e:
            messagebox.showerror("Error", f"Could not capture image: {e}")

    def capture_and_calibrate(self):
        try:
            image_path = 'calibration_capture.jpg'
            preview_path = 'calibrated_plate_preview.jpg'

            with picamera.PiCamera() as camera:
                camera.resolution = (1296, 972)
                camera.iso = 100
                camera.zoom = (0.35, 0.3, 0.3, 0.2)
                time.sleep(2)
                camera.capture(image_path)

            img = cv2.imread(image_path)
            if img is None:
                messagebox.showerror("Error", "Failed to load calibration image.")
                return

            warped = self.apply_warp(img)

            # Draw 8x12 grid overlay
            row_step = self.target_height / 8.0
            col_step = self.target_width / 12.0

            for r in range(1, 8):
                y = int(r * row_step)
                cv2.line(warped, (0, y), (self.target_width, y), (0, 255, 0), 1)

            for c in range(1, 12):
                x = int(c * col_step)
                cv2.line(warped, (x, 0), (x, self.target_height), (0, 255, 0), 1)

            cv2.imwrite(preview_path, warped)

            # Open preview image and clean up via subprocess call
            Image.open(preview_path).show()
            time.sleep(1)
            call(["rm", "-f", image_path, preview_path])

            self.info_label.config(text="Calibration preview generated with grid overlay.")

        except Exception as e:
            messagebox.showerror("Error", f"Calibration capture failed: {e}")


def main():
    root = tk.Tk()
    app = EZMiceApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
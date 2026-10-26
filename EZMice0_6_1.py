#!/usr/bin/env python3

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
        self.target_width = 600
        self.target_height = 400
        self.pmarg_x = 50
        self.pmarg_y = 40

        # Build the GUI
        self.create_widgets()

        # Camera Settings
        self.awbgains = (1.0, 2.3);

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
        arduino.close()

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
        #os.chdir(save_dir)

        with picamera.PiCamera() as camera:
            camera.resolution = (1296, 972)
            camera.exposure_mode = 'backlight'
            camera.aw_gains = self.awbgains
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

            self.recording = False
            camera.close()
            self.record_button.config(text='Record & Analyze')

            #command = "MP4Box -add {0}.h264 {1}.mp4".format(filename, filename)
            command = "ffmpeg -y -framerate 30 -i {0}.h264 -c copy {0}.mp4".format(filename)
            call([command], shell=True)

            if os.path.exists(filename + fileType):
                self.process_video(filename + fileType, f"{filename}_motion_indices.csv")

    def find_plate_corners(self, img):
        """Automatically finds the 4 corners of a rectangular well plate using OpenCV."""
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (15,15), 0) ###gray
        blurred = cv2.convertScaleAbs(blurred, alpha=1.5, beta=0) #blurred

        otsu_thresh, _ = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        edged1 = cv2.Canny(blurred, int(otsu_thresh/12), int(otsu_thresh/5))

        # Find contours
        contours1, _ = cv2.findContours(edged1.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        # Draw secondary layer
        cv2.drawContours(edged1, contours1, -1, (255, 255, 255), 20)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7,7))
        closed_mask = cv2.morphologyEx(edged1, cv2.MORPH_CLOSE, kernel)

        edged2 = cv2.Canny(closed_mask, 20, 50)
        contours2, _ = cv2.findContours(edged2.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        # Sort contours by area, keeping the largest ones
        contours = sorted(contours2, key=cv2.contourArea, reverse=True)
        i = 0
        for c in contours:
            # Approximate the contour into a polygon
            convex = cv2.convexHull(c)
            area = cv2.contourArea(c)
            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(convex, 0.02*peri, True)
            cv2.drawContours(img, [convex], -1, (0, 0, 255), 4)
            cv2.drawContours(img, [approx], -1, (0, 255, 0), 4)
            cv2.drawContours(img, contours, -1, (255, 0, 0), 4)

            # If the approximated contour has 4 points, we found our rectangle!
            if len(approx) == 4:
                # Reshape from (4, 1, 2) to (4, 2) and convert to float32
                pts = approx.reshape(4, 2).astype(np.float32)
                print("Successfully found plate corners.")
                self.plate_corners = self.order_points(pts)
                print(self.plate_corners)
                return img

        # Fallback to your default hardcoded corners if auto-detection fails
        print("Failed to find corners")
        return img
    def order_points(self, pts):
        """Helper to sort 4 points into: Top-Left, Top-Right, Bottom-Right, Bottom-Left."""
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

        # Define 96-well grid boundaries (12 columns x 8 rows)
        row_step = (self.target_height + 2*self.pmarg_y) / 8.0
        col_step = (self.target_width + 2*self.pmarg_x) / 12.0
        well_boxes = []
        for r in range(8):
            for c in range(12):
                x1 = int(self.pmarg_x + c * col_step)
                y1 = int(self.pmarg_y + r * row_step)
                x2 = int(self.pmarg_x + (c + 1) * col_step)
                y2 = int(self.pmarg_y + (r + 1) * row_step)
                well_boxes.append((r, c, x1, y1, x2, y2))

        with open(output_csv_path, mode='w', newline='') as csv_file:
            #CSV setup
            writer = csv.writer(csv_file)
            header = ['Frame', 'Time (s)']
            for r, c, _, _, _, _ in well_boxes:
                header.append(f"{chr(65+r)}{c+1}")
            writer.writerow(header)

            #Analysis video setup
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

                # Frame subtraction
                diff = cv2.absdiff(warp_frame, prev_warp)
                _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
                thresh_color = cv2.cvtColor(thresh, cv2.COLOR_GRAY2BGR)
                # Analysis video write
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
            image_path = self.save_path.get() + '/calibration_capture.jpg'
            preview_path = self.save_path.get()  + '/calibrated_plate_preview.jpg'

            with picamera.PiCamera() as camera:
                camera.resolution = (int(1296), int(972))
                camera.exposure_mode = 'backlight' #auto
                camera.awb_mode = 'auto'
                camera.iso = 100
                camera.zoom = (0.4, 0.3, 0.25, 0.25)
                time.sleep(2)
                self.awbgains = camera.awb_gains
                camera.awb_mode = 'off'
                camera.awb_gains = self.awbgains

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

            # Draw 8x12 grid overlay
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

            # Create the popup window
            popup = tk.Toplevel(self.master)
            popup.title("Plate Calibration Preview")

            # Convert image for Tkinter
            tk_img = ImageTk.PhotoImage(pil_img)

            # Display image in the popup
            popup_label = ttk.Label(popup, image=tk_img)
            popup_label.image = tk_img  # Keep reference to prevent garbage collection!
            popup_label.pack(padx=10, pady=10)

            # Add a close button so the user can dismiss it easily
            #ttk.Button(popup, text="Close", command=popup.destroy).pack(pady=5)
            # -------------------------------------------------------
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
    root = tk.Tk()
    app = EZMiceApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
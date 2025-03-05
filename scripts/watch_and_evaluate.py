#!/usr/bin/env python3

import time
import subprocess
import os
import sys
from datetime import datetime
import argparse
import signal

# Check if watchdog is installed, install if not
try:
    import watchdog
except ImportError:
    print("Installing watchdog package...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "watchdog"])

# Now that we've made sure watchdog is installed, import the specific modules
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class ReminderAgentWatcher(FileSystemEventHandler):
    def __init__(self, eval_script, input_file, output_file, html_file, cooldown=2.0):
        self.eval_script = eval_script
        self.input_file = input_file
        self.output_file = output_file
        self.html_file = html_file
        self.cooldown = cooldown
        self.last_run = 0
        self.is_running = False
        
    def on_modified(self, event):
        # Skip if not a Python file or inside __pycache__
        if event.is_directory or not event.src_path.endswith('.py') or '__pycache__' in event.src_path:
            return
            
        # Skip if we're already running or if we're within the cooldown period
        current_time = time.time()
        if self.is_running or (current_time - self.last_run < self.cooldown):
            return
            
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Change detected in {event.src_path}")
        self.run_evaluation()
        
    def run_evaluation(self):
        try:
            self.is_running = True
            self.last_run = time.time()
            
            # Print a separator for clarity
            print("\n" + "="*80)
            print(f"Running evaluation at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print("="*80)
            
            # Run the evaluation script
            cmd = [
                sys.executable,
                self.eval_script,
                '--input', self.input_file,
                '--output', self.output_file,
                '--html', self.html_file
            ]
            
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            
            # Stream the output in real-time
            for line in process.stdout:
                print(line, end='')
                
            process.wait()
            
            if process.returncode == 0:
                print("\n✅ Evaluation completed successfully!")
                print(f"📊 HTML Report: {os.path.abspath(self.html_file)}")
                
                # Try to open the HTML report automatically (platform-specific)
                try:
                    if sys.platform == 'darwin':  # macOS
                        subprocess.call(['open', self.html_file])
                    elif sys.platform == 'win32':  # Windows
                        os.startfile(self.html_file)
                    elif sys.platform.startswith('linux'):  # Linux
                        subprocess.call(['xdg-open', self.html_file])
                except:
                    print("Could not open HTML report automatically.")
            else:
                print("\n❌ Evaluation failed with errors.")
                
        except Exception as e:
            print(f"Error running evaluation: {e}")
        finally:
            self.is_running = False

def main():
    parser = argparse.ArgumentParser(description='Watch for changes in reminder agent code and run evaluation')
    parser.add_argument('--eval-script', default='scripts/evaluate_reminder.py', help='Path to evaluation script')
    parser.add_argument('--input', default='test_data/reminder_test_cases.json', help='Path to test cases JSON file')
    parser.add_argument('--output', default='evaluation_results.json', help='Path to output results JSON file')
    parser.add_argument('--html', default='evaluation_results.html', help='Path to output HTML report file')
    parser.add_argument('--watch-dir', default='agents/reminder_agent', help='Directory to watch for changes')
    parser.add_argument('--cooldown', type=float, default=2.0, help='Cooldown period in seconds between evaluations')
    args = parser.parse_args()
    
    # Create the event handler and observer
    event_handler = ReminderAgentWatcher(
        args.eval_script,
        args.input,
        args.output,
        args.html,
        args.cooldown
    )
    observer = Observer()
    
    # Schedule watching the directory
    watch_path = os.path.abspath(args.watch_dir)
    if not os.path.exists(watch_path):
        print(f"Error: Watch directory '{watch_path}' does not exist.")
        sys.exit(1)
        
    print(f"Starting to watch directory: {watch_path}")
    observer.schedule(event_handler, watch_path, recursive=True)
    observer.start()
    
    # Run an initial evaluation
    print("Running initial evaluation...")
    event_handler.run_evaluation()
    
    try:
        print("\nWatcher is active. Press Ctrl+C to stop.")
        print(f"Watching for changes in {watch_path}...")
        
        # Keep the main thread alive
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping watcher...")
        observer.stop()
    
    observer.join()

if __name__ == "__main__":
    main() 
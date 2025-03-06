#!/usr/bin/env python3
import os
import sys
import json
import argparse
import time
import builtins
import copy
import webbrowser
from datetime import datetime, timedelta
from contextlib import contextmanager

# Add the project root directory to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Project imports
import pytz
from utils.llm_utils import get_openai_client, chat_completion, parse_json_response
from agents.reminder_agent.reminder_agent import ReminderAgent
from agents.reminder_agent.reminder_db import format_datetime

# Install freezegun if not already installed
try:
    from freezegun import freeze_time
except ImportError:
    import subprocess
    import sys
    print("Installing freezegun package...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "freezegun"])
    from freezegun import freeze_time

# Try to import OpenAI, install if not available
try:
    import openai
except ImportError:
    import subprocess
    import sys
    print("Installing openai package...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "openai"])
    import openai

import json
from datetime import datetime, timedelta
import pytz
import time
import webbrowser
from agents.reminder_agent.reminder_agent import TimeAwareReminderAgent

@contextmanager
def time_machine(target_time):
    """Context manager to temporarily override time-related functions"""
    # Store original functions
    original_time_time = time.time
    
    # Create a mock time function
    def mock_time():
        return target_time.timestamp()
    
    # Create patches dictionary to track what we've patched
    patches = {}
    
    try:
        # Patch time.time()
        time.time = mock_time
        patches['time.time'] = True
        
        # Try to patch datetime in the reminder_agent module
        try:
            from agents.reminder_agent import reminder_agent
            import datetime as dt
            
            # Create a MockDatetime class
            class MockDatetime(dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    if tz:
                        return target_time.astimezone(tz)
                    return target_time
            
            # Only patch if the module has datetime attribute
            if hasattr(reminder_agent, 'datetime'):
                patches['reminder_agent.datetime'] = reminder_agent.datetime
                reminder_agent.datetime = MockDatetime
        except (ImportError, AttributeError) as e:
            print(f"Note: Could not patch datetime in reminder_agent module: {e}")
            # Continue even if we can't patch this
            pass
        
        # Yield control back to the caller
        yield
    finally:
        # Restore original functions
        time.time = original_time_time
        
        # Restore any other patches we made
        try:
            from agents.reminder_agent import reminder_agent
            if 'reminder_agent.datetime' in patches:
                reminder_agent.datetime = patches['reminder_agent.datetime']
        except (ImportError, AttributeError):
            pass

class TimeAwareReminderAgent:
    """Wrapper for ReminderAgent that enables testing with controlled time"""
    
    def __init__(self, reminder_agent):
        self.reminder_agent = reminder_agent
        self.current_time = None
    
    def set_current_time(self, current_time):
        """Set the current time for testing"""
        self.current_time = current_time
    
    def parse_reminder(self, message, action_type=None):
        """Parse a reminder message with the injected current time"""
        # Ensure action_type is a dictionary
        if action_type is None:
            action_type = {}
        elif not isinstance(action_type, dict):
            action_type = {"action_type": action_type}
        
        # Inject the current time into the action_type
        if self.current_time:
            # Format the current time in the expected format
            action_type["current_time"] = self.current_time.strftime("%b/%d/%Y %H:%M")
        
        # Call the underlying reminder agent
        result = self.reminder_agent.parse_reminder(message, action_type)
        
        # Handle the case where the result contains a 'reminders' array
        if result and 'reminders' in result and isinstance(result['reminders'], list):
            # For case_4, return the full list of reminders
            if len(result['reminders']) > 1:
                reminders_list = []
                for reminder in result['reminders']:
                    # Convert parsed_time to scheduled_time
                    if 'parsed_time' in reminder:
                        # Parse the time string
                        time_str = reminder['parsed_time']
                        try:
                            # Use the BRT timezone info
                            tzinfos = {"BRT": -3 * 3600}  # BRT is UTC-3
                            from dateutil import parser
                            dt = parser.parse(time_str, tzinfos=tzinfos)
                            reminder['scheduled_time'] = dt
                            del reminder['parsed_time']
                        except Exception as e:
                            print(f"Error parsing time: {e}")
                    reminders_list.append(reminder)
                return reminders_list
            # For single reminder in a reminders array, return just that reminder
            elif len(result['reminders']) == 1:
                reminder = result['reminders'][0]
                # Convert parsed_time to scheduled_time
                if 'parsed_time' in reminder:
                    # Parse the time string
                    time_str = reminder['parsed_time']
                    try:
                        # Use the BRT timezone info
                        tzinfos = {"BRT": -3 * 3600}  # BRT is UTC-3
                        from dateutil import parser
                        dt = parser.parse(time_str, tzinfos=tzinfos)
                        reminder['scheduled_time'] = dt
                        del reminder['parsed_time']
                    except Exception as e:
                        print(f"Error parsing time: {e}")
                return reminder
        
        # For regular single reminder results
        if result and 'parsed_time' in result:
            # Convert parsed_time to scheduled_time
            time_str = result['parsed_time']
            try:
                # Use the BRT timezone info
                tzinfos = {"BRT": -3 * 3600}  # BRT is UTC-3
                from dateutil import parser
                dt = parser.parse(time_str, tzinfos=tzinfos)
                result['scheduled_time'] = dt
                del result['parsed_time']
            except Exception as e:
                print(f"Error parsing time: {e}")
        
        return result

def compare_reminders_with_llm(expected, actual, message):
    """Compare expected and actual reminder results using LLM"""
    if not actual:
        return False, False, "No reminder parsed"
    
    # Format expected and actual times for display
    expected_time = expected.get('scheduled_time', '')
    if isinstance(expected_time, datetime):
        expected_time = expected_time.isoformat()
    
    actual_time = actual.get('scheduled_time', '')
    if isinstance(actual_time, datetime):
        actual_time = actual_time.isoformat()
    
    # Format the prompt for LLM comparison
    prompt = f"""
    Original message: "{message}"
    
    Expected reminder:
    - Title: "{expected.get('title', '')}"
    - Time: "{expected_time}"
    
    Actual reminder:
    - Title: "{actual.get('title', '')}"
    - Time: "{actual_time}"
    
    Evaluate if the actual reminder matches the expected one. Consider:
    1. Semantic title match: Do the titles refer to the same action/event? (e.g., "doctor appointment" matches "appointment with doctor")
    2. Time match: Are the times reasonably close? (within 5 minutes is acceptable)
    
    Return a JSON with:
    - title_match: true/false
    - time_match: true/false
    - explanation: Brief explanation of your evaluation
    """
    
    # Call the LLM
    result = chat_completion(
        messages=[
            {"role": "system", "content": "You are an evaluation assistant that determines if extracted reminders match the expected results."},
            {"role": "user", "content": prompt}
        ],
        model="gpt-3.5-turbo",
        temperature=0.1,
        response_format={"type": "json_object"}
    )
    
    # Parse the result
    eval_result = parse_json_response(result)
    if not eval_result:
        print("Warning: LLM evaluation failed, falling back to code comparison")
        # Fall back to code-based comparison
        title_match = expected.get('title', '').lower() == actual.get('title', '').lower()
        
        # Simple time comparison
        expected_time = expected.get('scheduled_time')
        actual_time = actual.get('scheduled_time')
        time_match = False
        
        if isinstance(expected_time, str) and isinstance(actual_time, str):
            # Simple string comparison for dates
            time_match = expected_time == actual_time
        elif expected_time and actual_time:
            # Both are datetime objects, compare with tolerance
            time_diff = abs((expected_time - actual_time).total_seconds())
            time_match = time_diff <= 300  # 5 minutes
            
        return title_match, time_match, "Fallback code comparison"
    
    # Extract the evaluation results
    title_match = eval_result.get('title_match', False)
    time_match = eval_result.get('time_match', False)
    explanation = eval_result.get('explanation', "No explanation provided")
    
    return title_match, time_match, explanation

def evaluate_test_case(agent, test_case, case_number=None):
    """Evaluate a single test case"""
    # Get test case data
    message = test_case.get('message', '')
    expected_result = test_case.get('expected_result', {})
    notes = test_case.get('notes', '')
    datetime_str = test_case.get('current_time', None)
    
    case_label = f"Case {case_number}: " if case_number else ""
    print(f"\n{case_label}{message}")
    if notes:
        print(f"Notes: {notes}")
    
    # Set the current time for the agent if provided
    current_time = None
    if datetime_str:
        try:
            current_time = datetime.fromisoformat(datetime_str)
            # Make sure it's timezone-aware
            if current_time.tzinfo is None:
                current_time = pytz.timezone('America/Sao_Paulo').localize(current_time)
            print(f"Current time: {current_time}")
        except ValueError:
            print(f"Warning: Invalid datetime format in test case: {datetime_str}")
    
    # Create a time-aware reminder agent for testing
    time_aware_agent = TimeAwareReminderAgent(agent)
    if current_time:
        time_aware_agent.set_current_time(current_time)
    
    # Call LLM to parse the reminder
    result = time_aware_agent.parse_reminder(message)
    
    # Convert result to expected format
    if result and 'title' in result and 'scheduled_time' in result:
        formatted_result = {
            'title': result['title'],
            'scheduled_time': result['scheduled_time']
        }
        # Preserve reminders field for multi-reminder tests
        if 'reminders' in result:
            formatted_result['reminders'] = result['reminders']
    else:
        formatted_result = None
    
    # Use LLM to compare results
    if not formatted_result:
        success = False
        error_message = "No valid reminder extracted"
        title_match = False
        time_match = False
        explanation = "Parser returned no result"
    else:
        title_match, time_match, explanation = compare_reminders_with_llm(
            expected_result, formatted_result, message
        )
        success = title_match and time_match
        
        if success:
            error_message = "Success"
        else:
            if not title_match and not time_match:
                error_message = "Both title and time mismatch"
            elif not title_match:
                error_message = "Title mismatch"
            else:
                error_message = "Time mismatch"
    
    # Print result
    print(f"Expected: {expected_result}")
    print(f"Got:      {formatted_result}")
    print(f"Evaluation: {explanation}")
    print(f"Result:   {'✅ PASS' if success else '❌ FAIL'} - {error_message}")
    
    return {
        'message': message,
        'expected': expected_result,
        'actual': formatted_result,
        'success': success,
        'error': error_message,
        'title_match': title_match if formatted_result else False,
        'time_match': time_match if formatted_result else False,
        'explanation': explanation if formatted_result else "No reminder parsed"
    }

def main():
    parser = argparse.ArgumentParser(description='Evaluate reminder parsing')
    parser.add_argument('--test-file', type=str, default='agents/reminder_agent/reminder_test_dataset.json',
                        help='Path to the test data JSON file')
    parser.add_argument('--output-dir', type=str, default='test_results',
                        help='Directory to save results')
    args = parser.parse_args()
    
    # Create output directory if it doesn't exist
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)
    
    # Load test cases
    print(f"Loading test cases from {args.test_file}")
    with open(args.test_file, 'r', encoding='utf-8') as f:
        test_cases = json.load(f)
    
    # Check if test_cases is a list directly or nested under a key
    if isinstance(test_cases, dict) and 'test_cases' in test_cases:
        test_cases = test_cases.get('test_cases', [])
    
    if not test_cases:
        print("No test cases found in the file")
        return
    
    # Create agent for testing
    agent = ReminderAgent()
    
    # Process each test case
    results = []
    passed = 0
    failed = 0
    
    print("\n" + "=" * 70)
    print("=" * 80)
    for i, test_case in enumerate(test_cases):
        print(f"\nEvaluating test case: {test_case.get('id', f'case_{i+1}')}")
        print("=" * 70)
        print("=" * 80)
        
        # Ensure test case has expected_result
        if 'expected_result' not in test_case or not test_case['expected_result']:
            # If missing, create a default one based on the message content
            # This is just for testing - in production, you'd want proper test cases
            test_case['expected_result'] = {
                'title': test_case.get('expected_title', ''),
                'scheduled_time': test_case.get('expected_time', '')
            }
        
        # Evaluate the test case
        result = evaluate_test_case(agent, test_case, i+1)
        if result.get('success', False):
            passed += 1
        else:
            failed += 1
        results.append(result)
    
    # Calculate overall results
    total = passed + failed
    success_rate = (passed / total) * 100 if total > 0 else 0
    
    print("\n" + "=" * 70)
    print("=" * 75)
    print(f"Overall results: {passed}/{total} passed ({success_rate:.2f}%)")
    print("=" * 70)
    print("=" * 75)
    
    # Create final results dictionary
    final_results = {
        'summary': {
            'success_rate': success_rate,
            'total_cases': total,
            'passed_cases': passed,
            'failed_cases': failed,
        },
        'detailed_results': results,
        'timestamp': datetime.now().isoformat()
    }
    
    # Save results to file
    output_file = os.path.join(args.output_dir, 'reminder_eval_results.json')
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(final_results, f, indent=2, default=json_serializable)
    print(f"Results saved to {output_file}")
    
    # Generate HTML report
    html_report = generate_html_report(results)
    html_file = os.path.join(args.output_dir, 'reminder_eval_results.html')
    with open(html_file, 'w', encoding='utf-8') as f:
        f.write(html_report)
    print(f"HTML report generated at {html_file}")
    
    # Open the HTML report in a browser
    print(f"Opening HTML report in browser: {os.path.abspath(html_file)}")
    webbrowser.open('file://' + os.path.abspath(html_file))

def json_serializable(obj):
    """Convert datetime objects to ISO format strings for JSON serialization."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")

def generate_html_report(results):
    """Generate HTML report from test results"""
    test_rows = []
    
    for idx, result in enumerate(results):
        # Skip non-dictionary results or handle them appropriately
        if not isinstance(result, dict):
            # Log the problematic entry and continue
            print(f"Warning: Skipping non-dictionary result at index {idx}: {result}")
            continue
            
        # Extract data
        message = result.get('message', '')
        expected = result.get('expected', {})
        actual = result.get('actual', {})
        success = result.get('success', False)
        error = result.get('error', '')
        explanation = result.get('explanation', '')
        
        # Prepare data for display
        expected_display = expected.copy() if expected else {}
        actual_display = actual.copy() if actual else {}
        
        # Convert datetime objects to string for display
        if expected_display and 'scheduled_time' in expected_display and expected_display['scheduled_time']:
            if isinstance(expected_display['scheduled_time'], datetime):
                expected_display['scheduled_time_str'] = expected_display['scheduled_time'].isoformat()
        
        if actual_display and 'scheduled_time' in actual_display and actual_display['scheduled_time']:
            if isinstance(actual_display['scheduled_time'], datetime):
                actual_display['scheduled_time_str'] = actual_display['scheduled_time'].isoformat()
        
        # Convert to JSON string for display, handling datetime objects
        expected_raw = json.dumps(expected_display, indent=2, default=json_serializable) if expected_display else "Not available"
        actual_raw = json.dumps(actual_display, indent=2, default=json_serializable) if actual_display else "Not available"
        
        # Create HTML row
        row = f"""
        <tr class="{'success' if success else 'failure'}">
            <td>{idx + 1}</td>
            <td><pre>{message}</pre></td>
            <td><pre>{expected_raw}</pre></td>
            <td><pre>{actual_raw}</pre></td>
            <td>{explanation}</td>
            <td>{'✅ PASS' if success else '❌ FAIL'}</td>
            <td>{error}</td>
        </tr>
        """
        test_rows.append(row)
    
    # Handle empty results
    if not test_rows:
        test_rows = ["<tr><td colspan='7'>No results to display</td></tr>"]
        
    # Count valid results for summary
    valid_results = [r for r in results if isinstance(r, dict)]
    num_results = len(valid_results)
    num_passed = sum(1 for r in valid_results if r.get('success', False))
    
    # Create HTML with improved styling for table fit
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Reminder Agent Evaluation Results</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 20px;
                line-height: 1.6;
            }}
            h1 {{
                color: #2c3e50;
                border-bottom: 2px solid #eee;
                padding-bottom: 10px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin: 20px 0;
                font-size: 13px;
                table-layout: fixed; /* Fixed layout for better control */
            }}
            th, td {{
                padding: 8px 10px;
                border-bottom: 1px solid #ddd;
                text-align: left;
                word-wrap: break-word; /* Allow words to break */
                overflow: hidden;
            }}
            th {{
                background-color: #f8f9fa;
                font-weight: bold;
            }}
            th:nth-child(1) {{ width: 3%; }} /* # column */
            th:nth-child(2) {{ width: 20%; }} /* Message column */
            th:nth-child(3) {{ width: 20%; }} /* Expected column */
            th:nth-child(4) {{ width: 20%; }} /* Actual column */
            th:nth-child(5) {{ width: 20%; }} /* Explanation column */
            th:nth-child(6) {{ width: 7%; }} /* Result column */
            th:nth-child(7) {{ width: 10%; }} /* Error column */
            
            .summary {{
                background-color: #f8f9fa;
                border-left: 4px solid #5bc0de;
                padding: 15px;
                margin-bottom: 20px;
            }}
            .success td {{
                background-color: #f0fff0;
            }}
            .failure td {{
                background-color: #fff0f0;
            }}
            pre {{
                background-color: #f8f9fa;
                padding: 6px;
                border-radius: 4px;
                overflow-x: auto;
                font-size: 12px; /* Smaller font for code */
                max-height: 120px; /* Limit height with scroll */
                margin: 0;
            }}
            /* Make the table responsive */
            @media screen and (max-width: 1200px) {{
                table {{
                    font-size: 12px;
                }}
                th, td {{
                    padding: 6px 8px;
                }}
                pre {{
                    font-size: 11px;
                    padding: 4px;
                }}
            }}
        </style>
    </head>
    <body>
        <h1>Reminder Agent Evaluation Results</h1>
        <div class="summary">
            <h2>Summary</h2>
            <p>Total test cases: {num_results}</p>
            <p>Passed: {num_passed}</p>
            <p>Failed: {num_results - num_passed}</p>
            <p>Success rate: {(num_passed / num_results * 100) if num_results > 0 else 0:.2f}%</p>
        </div>
        <table>
            <thead>
                <tr>
                    <th>#</th>
                    <th>Message</th>
                    <th>Expected</th>
                    <th>Actual</th>
                    <th>Explanation</th>
                    <th>Result</th>
                    <th>Error</th>
                </tr>
            </thead>
            <tbody>
                {''.join(test_rows)}
            </tbody>
        </table>
    </body>
    </html>
    """
    return html

if __name__ == '__main__':
    main()

import json
import os
import sys
import openai
from datetime import datetime, timedelta
import pytz
import argparse
from contextlib import contextmanager
import builtins
import time
import copy
import webbrowser

# Import your reminder agent
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.reminder_agent.reminder_agent import ReminderAgent

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
            import reminder_agent
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
            import reminder_agent
            if 'reminder_agent.datetime' in patches:
                reminder_agent.datetime = patches['reminder_agent.datetime']
        except (ImportError, AttributeError):
            pass

class TimeAwareReminderAgent:
    """Wrapper around ReminderAgent that allows injecting a specific time"""
    
    def __init__(self, reminder_agent):
        self.reminder_agent = reminder_agent
        self.current_time = None
    
    def set_current_time(self, current_time):
        """Set the current time to use for parsing"""
        self.current_time = current_time
    
    def parse_reminder(self, message, action_type):
        """Parse a reminder message with the specified current time"""
        # Store original methods that might be used to get current time
        import datetime as dt
        import time
        
        original_dt_now = dt.datetime.now
        original_time_time = time.time
        
        try:
            # Only monkey patch if we have a current_time set
            if self.current_time:
                # Create mock functions
                def mock_dt_now(*args, **kwargs):
                    if 'tz' in kwargs:
                        return self.current_time.astimezone(kwargs['tz'])
                    return self.current_time
                
                def mock_time_time():
                    return self.current_time.timestamp()
                
                # Monkey patch
                dt.datetime.now = mock_dt_now
                time.time = mock_time_time
            
            # Call the original parse_reminder method
            return self.reminder_agent.parse_reminder(message, action_type)
        finally:
            # Restore original methods
            dt.datetime.now = original_dt_now
            time.time = original_time_time

def evaluate_reminder_case(reminder_agent, test_case, current_time):
    """Evaluate a single test case using LLM as judge"""
    message = test_case["message"]
    expected = test_case["expected"]
    
    # Get the actual result from the reminder agent
    try:
        print(f"Parsing message: {message}")
        
        # Use freezegun to freeze time at the current_time
        with freeze_time(current_time):
            print(f"Frozen time check: {datetime.now()}")
            parsed_result = reminder_agent.parse_reminder(message, {"current_time": current_time.strftime("%b/%d/%Y %H:%M")})
            print(f"Parsed result: {json.dumps(parsed_result, indent=2)}")
    except Exception as e:
        print(f"Error parsing reminder: {str(e)}")
        return {
            "id": test_case["id"],
            "message": message,
            "current_time": test_case.get("current_time", "Not specified"),
            "description": test_case.get("description", ""),
            "passed": False,
            "error": f"Exception: {str(e)}",
            "expected": expected,
            "actual": None,
            "reasoning": f"Failed to parse reminder due to exception: {str(e)}"
        }
    
    # Process parsed result
    try:
        # Check if we're expecting multiple reminders
        if isinstance(expected, list):
            # Handle multiple reminders case
            reminders = []
            
            # Check if the result has a reminders array
            if "reminders" in parsed_result and isinstance(parsed_result["reminders"], list):
                reminders = parsed_result["reminders"]
            # If the result is a single reminder but we expected multiple
            elif "title" in parsed_result and "parsed_time" in parsed_result:
                reminders = [parsed_result]
            
            # Convert all reminders to a standard format for comparison
            actual_reminders = []
            for reminder in reminders:
                if "title" not in reminder or "parsed_time" not in reminder:
                    print(f"Warning: Reminder missing required fields: {reminder}")
                    continue
                
                actual_reminders.append({
                    "title": reminder["title"],
                    "parsed_time": reminder["parsed_time"]
                })
            
            # Compare expected and actual reminders
            # First, check if we have the same number of reminders
            if len(expected) != len(actual_reminders):
                return {
                    "id": test_case["id"],
                    "message": message,
                    "current_time": test_case.get("current_time", "Not specified"),
                    "description": test_case.get("description", ""),
                    "passed": False,
                    "expected": expected,
                    "actual": {"reminders": actual_reminders},
                    "reasoning": f"Expected {len(expected)} reminders, but got {len(actual_reminders)}"
                }
            
            # Compare each reminder
            matches = []
            for exp, act in zip(expected, actual_reminders):
                title_match = exp["title"].lower() == act["title"].lower()
                try:
                    # Parse dates in a way that doesn't use isoformat
                    exp_time_str = exp["parsed_time"]
                    act_time_str = act["parsed_time"]
                    
                    # Display the actual time string without trying to parse it
                    act["display_time"] = act_time_str
                    
                    # Clean the time strings for comparison
                    if " BRT" in act_time_str:
                        act_time_str = act_time_str.replace(" BRT", "")
                    
                    # Try to parse the expected time
                    if "T" in exp_time_str:
                        # ISO format (2025-03-05T15:17:00-03:06)
                        exp_time = datetime.fromisoformat(exp_time_str.replace('Z', '+00:00'))
                    else:
                        # Try custom format (Mar/5/2025 15:17)
                        try:
                            exp_time = datetime.strptime(exp_time_str, "%b/%d/%Y %H:%M")
                            exp_time = current_time.tzinfo.localize(exp_time) if current_time.tzinfo else exp_time
                        except:
                            exp_time = None
                    
                    # Try to parse the actual time
                    try:
                        # Extract components from format like "Mar/5/2025 15:17"
                        month_str, day_str, rest = act_time_str.split('/')
                        year_str, time_part = rest.split(' ')
                        hour_str, minute_str = time_part.split(':')
                        
                        # Convert month name to number
                        month_map = {
                            "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
                            "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12
                        }
                        month = month_map.get(month_str, 1)
                        
                        # Create datetime
                        act_time = datetime(
                            year=int(year_str),
                            month=month,
                            day=int(day_str),
                            hour=int(hour_str),
                            minute=int(minute_str),
                            tzinfo=current_time.tzinfo if hasattr(current_time, 'tzinfo') else None
                        )
                    except Exception as e:
                        print(f"Error parsing actual time: {e}")
                        act_time = None
                    
                    # Check if both times could be parsed
                    if exp_time and act_time:
                        time_diff = abs((exp_time - act_time).total_seconds())
                        time_match = time_diff <= 60  # Allow 1 minute difference
                    else:
                        time_match = False
                except Exception as e:
                    print(f"Error comparing times: {e}")
                    time_match = False
                
                matches.append({"title_match": title_match, "time_match": time_match})
            
            # If all reminders match, the test passes
            all_match = all(m["title_match"] and m["time_match"] for m in matches)
            
            # Format the reminders for display
            display_reminders = []
            for reminder in actual_reminders:
                display_reminders.append({
                    "title": reminder["title"],
                    "parsed_time": reminder["parsed_time"]
                })
            
            reasoning = "Match details:\n"
            for i, (exp, act, match) in enumerate(zip(expected, actual_reminders, matches)):
                reasoning += f"\nReminder #{i+1}:\n"
                reasoning += f"- Expected: {exp['title']} at {exp['parsed_time']}\n"
                reasoning += f"- Actual: {act['title']} at {act['parsed_time']}\n"
                reasoning += f"- Title match: {match['title_match']}, Time match: {match['time_match']}\n"
            
            # Use LLM for detailed comparison and reasoning if available
            if os.getenv("OPENAI_API_KEY") and compare_with_llm:
                judgment = compare_with_llm(expected, actual_reminders, matches, all_match)
                reasoning = judgment.get("reasoning", reasoning)
                
                # Override with our own calculation to ensure strictness
                judgment["passed"] = all_match
                
                return {
                    "id": test_case["id"],
                    "message": message,
                    "current_time": test_case.get("current_time", "Not specified"),
                    "description": test_case.get("description", ""),
                    "passed": all_match,
                    "expected": expected,
                    "actual": {"reminders": display_reminders},
                    "reasoning": reasoning
                }
            
            return {
                "id": test_case["id"],
                "message": message,
                "current_time": test_case.get("current_time", "Not specified"),
                "description": test_case.get("description", ""),
                "passed": all_match,
                "expected": expected,
                "actual": {"reminders": display_reminders},
                "reasoning": reasoning
            }
        else:
            # Handle single reminder case
            if "reminders" in parsed_result and isinstance(parsed_result["reminders"], list) and len(parsed_result["reminders"]) > 0:
                # We got multiple reminders but expected a single one, use the first reminder
                single_reminder = parsed_result["reminders"][0]
            else:
                # Expected a single reminder
                single_reminder = parsed_result
            
            # Check required fields
            if "title" not in single_reminder:
                return {
                    "id": test_case["id"],
                    "message": message,
                    "current_time": test_case.get("current_time", "Not specified"),
                    "description": test_case.get("description", ""),
                    "passed": False,
                    "expected": expected,
                    "actual": single_reminder,
                    "reasoning": "Missing title in the result"
                }
            
            if "parsed_time" not in single_reminder:
                return {
                    "id": test_case["id"],
                    "message": message,
                    "current_time": test_case.get("current_time", "Not specified"),
                    "description": test_case.get("description", ""),
                    "passed": False,
                    "expected": expected,
                    "actual": single_reminder,
                    "reasoning": "Missing parsed_time in the result"
                }
            
            # Extract title and time
            title = single_reminder["title"]
            parsed_time = single_reminder["parsed_time"]
            
            # Create actual_reminder for comparison
            actual_reminder = {
                "title": title,
                "parsed_time": parsed_time
            }
            
            # Check if title matches
            title_match = expected["title"].lower() == title.lower()
            
            # Check if time matches
            try:
                # For display purposes, don't try to parse the time
                time_match = False
                exp_time_str = expected["parsed_time"]
                act_time_str = parsed_time
                
                # Clean the time strings
                if " BRT" in act_time_str:
                    act_time_str = act_time_str.replace(" BRT", "")
                
                # Parse expected time
                if "T" in exp_time_str:
                    # ISO format
                    exp_time = datetime.fromisoformat(exp_time_str.replace('Z', '+00:00'))
                else:
                    # Try custom format
                    exp_time = datetime.strptime(exp_time_str, "%b/%d/%Y %H:%M")
                    exp_time = current_time.tzinfo.localize(exp_time) if current_time.tzinfo else exp_time
                
                # Parse actual time
                try:
                    # Extract components
                    month_str, day_str, rest = act_time_str.split('/')
                    year_str, time_part = rest.split(' ')
                    hour_str, minute_str = time_part.split(':')
                    
                    # Convert month name to number
                    month_map = {
                        "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
                        "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12
                    }
                    month = month_map.get(month_str, 1)
                    
                    # Create datetime
                    act_time = datetime(
                        year=int(year_str),
                        month=month,
                        day=int(day_str),
                        hour=int(hour_str),
                        minute=int(minute_str),
                        tzinfo=current_time.tzinfo if hasattr(current_time, 'tzinfo') else None
                    )
                    
                    # Check if times match within acceptable range
                    time_diff = abs((exp_time - act_time).total_seconds())
                    time_match = time_diff <= 60  # Allow 1 minute difference
                except Exception as e:
                    print(f"Error parsing actual time: {e}")
                    time_match = False
            except Exception as e:
                print(f"Error comparing times: {str(e)}")
                time_match = False
            
            # Overall match
            passed = title_match and time_match
            
            # Prepare reasoning
            reasoning = f"Title match: {title_match}, Time match: {time_match}\n\n"
            reasoning += f"Expected: {expected['title']} at {expected['parsed_time']}\n"
            reasoning += f"Actual: {title} at {parsed_time}"
            
            return {
                "id": test_case["id"],
                "message": message,
                "current_time": test_case.get("current_time", "Not specified"),
                "description": test_case.get("description", ""),
                "passed": passed,
                "expected": expected,
                "actual": actual_reminder,
                "reasoning": reasoning
            }
    except Exception as e:
        print(f"Error during evaluation: {str(e)}")
        # Return a default failed result
        return {
            "id": test_case["id"],
            "message": message,
            "current_time": test_case.get("current_time", "Not specified"),
            "description": test_case.get("description", ""),
            "passed": False,
            "error": f"Exception during evaluation: {str(e)}",
            "expected": expected,
            "actual": parsed_result if 'parsed_result' in locals() else None,
            "reasoning": f"An error occurred during evaluation: {str(e)}"
        }

def compare_with_llm(expected, actual, match_details, all_match):
    """Compare expected and actual reminders using LLM"""
    try:
        # Prepare prompt for LLM
        expected_str = json.dumps(expected, indent=2)
        actual_str = json.dumps(actual, indent=2)
        
        prompt = f"""
        You are evaluating a reminder parsing system. Compare the expected reminders with the actual parsed reminders and determine if they match.
        
        Expected reminders:
        {expected_str}
        
        Actual reminders:
        {actual_str}
        
        For each reminder, check if:
        1. The title matches exactly (ignoring case)
        2. The parsed time is accurate (within 1 minute)
        
        Provide a detailed reasoning for your evaluation, analyzing each reminder individually.
        
        Return your response as a JSON object with the following format:
        {{
          "passed": true/false,
          "reasoning": "Your detailed analysis here"
        }}
        """
        
        # Create OpenAI client
        client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        
        # Call LLM API
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are an evaluation assistant for reminder parsing systems. Provide responses in JSON format."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        
        # Parse response
        result = json.loads(response.choices[0].message.content)
        
        # Override with our own calculation to ensure strictness
        result["passed"] = all_match
        
        # If we're overriding to false, add a note
        if not all_match and result.get("passed", False):
            result["reasoning"] = "OVERRIDE: " + result.get("reasoning", "") + "\n\nNote: The evaluation was overridden to FAIL because not all reminders match exactly."
        
        return result
    except Exception as e:
        print(f"Error using LLM for evaluation: {str(e)}")
        # Return a fallback judgment
        return {
            "passed": all_match,
            "reasoning": f"Reminder comparison completed with automated matching. Error using LLM for detailed analysis: {str(e)}"
        }

def main():
    """Run the evaluation"""
    parser = argparse.ArgumentParser(description='Evaluate reminder agent on test cases')
    parser.add_argument('--test-cases', default='agents/reminder_agent/reminder_test_dataset.json',
                      help='Path to test cases JSON file')
    parser.add_argument('--output', default='test_results/reminder_eval_results.json',
                      help='Path to output JSON file')
    parser.add_argument('--html', default='test_results/reminder_eval_results.html',
                      help='Path to output HTML report file')
    parser.add_argument('--delay', type=int, default=2,
                      help='Delay in seconds between test cases to avoid API rate limiting')
    parser.add_argument('--no-open', action='store_true',
                      help='Do not automatically open the HTML report')
    args = parser.parse_args()
    
    # Check if test cases file exists
    if not os.path.exists(args.test_cases):
        print(f"Error: Test cases file not found: {args.test_cases}")
        print(f"Please ensure the test cases file exists at the specified path.")
        return 1
    
    # Load test cases
    print(f"Loading test cases from {args.test_cases}")
    with open(args.test_cases, 'r', encoding='utf-8') as f:
        test_cases = json.load(f)
    
    # Ensure we have test cases
    if not test_cases:
        print("Error: No test cases found in the test cases file.")
        return 1

    # Initialize the reminder agent
    reminder_agent = TimeAwareReminderAgent(ReminderAgent())
    
    # Evaluate each test case
    results = []
    for i, test_case in enumerate(test_cases):
        print("\n" + "=" * 80)
        print(f"Evaluating test case: {test_case['id']}")
        print("=" * 80)
        
        # Parse the current time
        current_time_str = test_case.get('current_time')
        if not current_time_str:
            print("Warning: No current_time specified in test case, using now as default")
            current_time = datetime.now(pytz.UTC)
        else:
            try:
                current_time = datetime.fromisoformat(current_time_str)
                if current_time.tzinfo is None:
                    # Apply default timezone if none specified
                    current_time = current_time.replace(tzinfo=pytz.UTC)
            except ValueError:
                print(f"Error: Invalid current_time format in test case: {current_time_str}")
                print("Using current time as fallback")
                current_time = datetime.now(pytz.UTC)
        
        # Evaluate the test case
        result = evaluate_reminder_case(reminder_agent, test_case, current_time)
        results.append(result)
        
        # Add delay between API calls to avoid rate limiting/caching issues
        if i < len(test_cases) - 1:  # Don't delay after the last test
            print(f"\nWaiting {args.delay} seconds before the next test case to avoid API rate limiting...")
            time.sleep(args.delay)
    
    # Calculate overall metrics and save results
    total_cases = len(results)
    passed_cases = sum(1 for r in results if r.get("passed", False))
    failed_cases = total_cases - passed_cases
    success_rate = passed_cases / total_cases if total_cases > 0 else 0
    
    print("\n=============================================================================")
    print(f"Overall results: {passed_cases}/{total_cases} passed ({success_rate:.2%})")
    print("=============================================================================")
    
    # Combine results into a single object
    final_results = {
        "success_rate": success_rate,
        "total_cases": total_cases,
        "passed_cases": passed_cases,
        "failed_cases": failed_cases,
        "detailed_results": results
    }
    
    # Save results
    save_results(final_results, args.output, args.html)
    
    print(f"\nResults saved to {args.output}")
    print(f"HTML report generated at {args.html}")
    
    # Open the HTML report in the default browser
    if not args.no_open:
        try:
            html_path = os.path.abspath(args.html)
            print(f"Opening HTML report in browser: {html_path}")
            webbrowser.open('file://' + html_path)
        except Exception as e:
            print(f"Could not open HTML report automatically: {e}")
    
    # Return exit code based on success rate
    return 0 if success_rate == 1.0 else 1

def generate_html_report(results):
    """Generate an HTML report from the evaluation results"""
    # If results is from history array, use the provided results object
    # No need to extract the latest since we're passing the specific result
    
    # Get current timestamp for the report
    execution_time = results.get("timestamp", datetime.now().isoformat())
    try:
        execution_time = datetime.fromisoformat(execution_time).strftime("%Y-%m-%d %H:%M:%S")
    except:
        pass
    
    # Format the results for display
    formatted_results = []
    for result in results["detailed_results"]:
        # Get expected and actual for formatting
        expected = result.get("expected", {})
        actual = result.get("actual", {})
        
        # Format expected for table view - using consistent date format
        if isinstance(expected, list):
            expected_formatted = "<ul>"
            for exp in expected:
                expected_formatted += f"<li>{exp['title']} at {exp['parsed_time']}</li>"
            expected_formatted += "</ul>"
        else:
            expected_formatted = f"{expected.get('title', 'Not available')} at {expected.get('parsed_time', 'Not available')}"
        
        # Format actual for table view - using consistent format
        if isinstance(actual, dict):
            if "reminders" in actual and isinstance(actual["reminders"], list):
                # Multiple reminders case
                actual_formatted = "<ul>"
                for rem in actual["reminders"]:
                    title = rem.get("title", "Not available")
                    parsed_time = rem.get("parsed_time", "Not available")
                    actual_formatted += f"<li>{title} at {parsed_time}</li>"
                actual_formatted += "</ul>"
            else:
                # Single reminder case
                title = actual.get("title", "Not available")
                parsed_time = actual.get("parsed_time", "Not available")
                actual_formatted = f"{title} at {parsed_time}"
        else:
            actual_formatted = "Not available"
        
        # Format status
        status = "PASSED" if result.get("passed", False) else "FAILED"
        status_class = "success" if result.get("passed", False) else "danger"
        status_icon = "✓" if result.get("passed", False) else "✗"
        
        # Create display-friendly JSON versions for the detailed view
        # We'll keep the original structure but ensure date formats are consistent
        expected_display = copy.deepcopy(expected)
        actual_display = copy.deepcopy(actual) if actual else None
        
        # Get the raw expected and actual JSON for the detailed view
        expected_raw = json.dumps(expected_display, indent=2)
        actual_raw = json.dumps(actual_display, indent=2) if actual_display else "Not available"
        
        # Format the reasoning with consistent date formats
        reasoning = result.get("reasoning", "No reasoning provided")
        
        # Create our own formatted reasoning for display consistency
        if "expected" in result and "actual" in result and result["actual"]:
            # Handle single reminder case
            if not isinstance(expected, list) and not (isinstance(actual, dict) and "reminders" in actual):
                expected_title = expected.get("title", "")
                expected_time = expected.get("parsed_time", "")
                
                actual_title = actual.get("title", "")
                actual_time = actual.get("parsed_time", "")
                
                title_match = expected_title.lower() == actual_title.lower() if expected_title and actual_title else False
                # For display purposes, we compare the original time fields
                time_match = expected_time == actual_time if expected_time and actual_time else False
                
                formatted_reasoning = f"Title match: {title_match}, Time match: {time_match}\n\n"
                formatted_reasoning += f"Expected: {expected_title} at {expected_time}\n"
                formatted_reasoning += f"Actual: {actual_title} at {actual_time}"
                
                reasoning = formatted_reasoning
        
        # Replace newlines with <br> for HTML display
        reasoning = reasoning.replace("\n", "<br>")
        
        formatted_results.append({
            "id": result["id"],
            "message": result["message"],
            "current_time": result.get("current_time", "Not specified"),
            "expected_formatted": expected_formatted,
            "actual_formatted": actual_formatted,
            "expected_raw": expected_raw,
            "actual_raw": actual_raw,
            "status": status,
            "status_class": status_class,
            "status_icon": status_icon,
            "reasoning": reasoning
        })
    
    # Calculate summary statistics
    total = len(formatted_results)
    passed = sum(1 for r in formatted_results if r.get("passed", False))
    pass_rate = passed / total * 100 if total > 0 else 0
    
    # Generate HTML
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Reminder Agent Evaluation Results</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; }}
            h1, h2, h3 {{ color: #333; }}
            table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; }}
            th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
            th {{ background-color: #f2f2f2; }}
            
            /* Tab styles */
            .tab {{ overflow: hidden; border: 1px solid #ccc; background-color: #f1f1f1; margin-bottom: 20px; }}
            .tab button {{ background-color: inherit; float: left; border: none; outline: none; cursor: pointer; padding: 14px 16px; transition: 0.3s; }}
            .tab button:hover {{ background-color: #ddd; }}
            .tab button.active {{ background-color: #ccc; }}
            .tabcontent {{ display: none; padding: 6px 12px; border: 1px solid #ccc; border-top: none; }}
            #Overview {{ display: block; }}
            
            /* Table row styles */
            .success {{ background-color: #dff0d8; }}
            .danger {{ background-color: #f2dede; }}
            
            /* Detailed view styles */
            .details-container {{ margin-bottom: 30px; border: 1px solid #ddd; border-radius: 5px; overflow: hidden; }}
            .details-success {{ border-left: 8px solid #5cb85c; }}
            .details-danger {{ border-left: 8px solid #d9534f; }}
            .details-header {{ padding: 15px; background-color: #fff; }}
            .details-message {{ padding: 0 15px; margin: 10px 0; }}
            .details-section {{ padding: 15px; margin: 0; }}
            .details-code {{ background-color: #f5f5f5; padding: 15px; margin: 0; font-family: monospace; white-space: pre-wrap; }}
            .details-reasoning {{ background-color: #fcf8e3; padding: 15px; margin: 0; }}
            .section-label {{ font-weight: bold; margin-bottom: 5px; display: block; }}
            .summary {{ margin: 20px 0; }}
            .execution-time {{ color: #666; font-style: italic; margin-bottom: 20px; }}
        </style>
    </head>
    <body>
        <h1>Reminder Agent Evaluation Results</h1>
        <div class="execution-time">Report generated on: {execution_time}</div>
        
        <div class="tab">
            <button class="tablinks active" onclick="openTab(event, 'Overview')">Overview</button>
            <button class="tablinks" onclick="openTab(event, 'Details')">Detailed Results</button>
        </div>
        
        <div id="Overview" class="tabcontent">
            <div class="summary">
                <h2>Summary</h2>
                <p>Total test cases: {total}</p>
                <p>Passed: {passed} ({pass_rate:.1f}%)</p>
                <p>Failed: {total - passed} ({100 - pass_rate:.1f}%)</p>
            </div>
            
            <h2>Results Overview</h2>
            <table>
                <tr>
                    <th>ID</th>
                    <th>Message</th>
                    <th>Current Time</th>
                    <th>Expected</th>
                    <th>Actual</th>
                    <th>Status</th>
                </tr>
    """
    
    for r in formatted_results:
        html += f"""
                <tr class="{r['status_class']}">
                    <td>{r['id']}</td>
                    <td>{r['message']}</td>
                    <td>{r['current_time']}</td>
                    <td>{r['expected_formatted']}</td>
                    <td>{r['actual_formatted']}</td>
                    <td>{r['status']}</td>
                </tr>
        """
    
    html += """
            </table>
        </div>
        
        <div id="Details" class="tabcontent">
            <h2>Detailed Test Results</h2>
    """
    
    for r in formatted_results:
        html += f"""
            <div class="details-container details-{r['status_class']}">
                <div class="details-header">
                    <h3>{r['id']} - {r['status_icon']} {r['status']}</h3>
                </div>
                
                <div class="details-message">
                    <span class="section-label">Message:</span> {r['message']}
                </div>
                
                <div class="details-section">
                    <span class="section-label">Expected:</span>
                </div>
                <pre class="details-code">{r['expected_raw']}</pre>
                
                <div class="details-section">
                    <span class="section-label">Actual:</span>
                </div>
                <pre class="details-code">{r['actual_raw']}</pre>
                
                <div class="details-section">
                    <span class="section-label">Reasoning:</span>
                </div>
                <div class="details-reasoning">{r['reasoning']}</div>
            </div>
        """
    
    html += """
        </div>
        
        <script>
            function openTab(evt, tabName) {
                var i, tabcontent, tablinks;
                
                // Hide all tab content
                tabcontent = document.getElementsByClassName("tabcontent");
                for (i = 0; i < tabcontent.length; i++) {
                    tabcontent[i].style.display = "none";
                }
                
                // Remove "active" class from all tab buttons
                tablinks = document.getElementsByClassName("tablinks");
                for (i = 0; i < tablinks.length; i++) {
                    tablinks[i].className = tablinks[i].className.replace(" active", "");
                }
                
                // Show the selected tab and add "active" class to the button
                document.getElementById(tabName).style.display = "block";
                evt.currentTarget.className += " active";
            }
        </script>
    </body>
    </html>
    """
    
    return html

def save_results(results, output_file, html_file):
    """Save results to JSON and HTML files"""
    # Create output directory if it doesn't exist
    output_dir = os.path.dirname(output_file)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    # Add timestamp to current results
    timestamp = datetime.now().isoformat()
    results_with_meta = {
        "timestamp": timestamp,
        "success_rate": results["success_rate"],
        "total_cases": results["total_cases"],
        "passed_cases": results["passed_cases"],
        "failed_cases": results["failed_cases"],
        "detailed_results": results["detailed_results"]
    }
    
    # Load existing results history if file exists
    history = []
    if os.path.exists(output_file):
        try:
            with open(output_file, 'r', encoding='utf-8') as f:
                existing_data = json.load(f)
                # Check if existing data is already a list or a single result
                if isinstance(existing_data, list):
                    history = existing_data
                else:
                    # If it's a single result object, convert to a list
                    history = [existing_data]
        except (json.JSONDecodeError, FileNotFoundError):
            # If file is corrupted or doesn't exist, start fresh
            history = []
    
    # Add current results to history
    history.append(results_with_meta)
    
    # Write updated history back to file
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
    
    # Generate and save HTML report (using only the most recent results)
    html_content = generate_html_report(results_with_meta)
    with open(html_file, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    return results_with_meta

if __name__ == "__main__":
    sys.exit(main())

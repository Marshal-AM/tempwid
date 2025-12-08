import os
import sys
import asyncio
import aiohttp
import httpx
import time
import json
import atexit
from datetime import datetime, timedelta
from threading import Lock
from typing import List, Dict, Optional
from pydantic import BaseModel
try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Fallback for Python < 3.9
    try:
        from backports.zoneinfo import ZoneInfo
    except ImportError:
        # Final fallback - use UTC
        ZoneInfo = None

from dotenv import load_dotenv
from system_prompt import SYSTEM_PROMPT
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

# Load environment variables from .env file
load_dotenv()

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pyngrok import ngrok

from pipecat.frames.frames import EndFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.google.gemini_live.llm_vertex import GeminiLiveVertexLLMService
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

from loguru import logger

# Configure logger
logger.remove(0)
logger.add(sys.stderr, level="DEBUG")

# Global variable to store the ngrok tunnel
ngrok_tunnel = None

# Global storage for guardrails (question-answer pairs)
guardrails_storage: List[Dict[str, str]] = []
guardrails_lock = Lock()

# FastAPI app
app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
DAILY_API_KEY = os.getenv("DAILY_API_KEY", "")
GOOGLE_CLOUD_PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT_ID", "")
GOOGLE_CLOUD_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
GOOGLE_VERTEX_CREDENTIALS = os.getenv("GOOGLE_VERTEX_CREDENTIALS", "")
MONGODB_URI = os.getenv("MONGODB_URI", "")

if not DAILY_API_KEY:
    raise ValueError("DAILY_API_KEY must be set")
if not GOOGLE_CLOUD_PROJECT_ID:
    raise ValueError("GOOGLE_CLOUD_PROJECT_ID must be set")
if not GOOGLE_VERTEX_CREDENTIALS:
    raise ValueError("GOOGLE_VERTEX_CREDENTIALS must be set")
if not MONGODB_URI:
    logger.warning("MONGODB_URI not set - user lookup functionality will be disabled")


def start_ngrok_tunnel(port=8000):
    """Start ngrok tunnel and return the public URL."""
    global ngrok_tunnel
    
    # Get ngrok auth token from environment or use default
    ngrok_auth_token = os.getenv("NGROK_AUTH_TOKEN")
    
    if ngrok_auth_token:
        # Set the authtoken
        ngrok.set_auth_token(ngrok_auth_token)
        logger.info("Using ngrok auth token from environment")
    else:
        logger.warning("NGROK_AUTH_TOKEN not set in environment, using free ngrok (may have limitations)")
    
    # Start the tunnel
    ngrok_tunnel = ngrok.connect(port, "http")
    
    # Get the public URL
    public_url = ngrok_tunnel.public_url
    
    logger.info("=" * 60)
    logger.info("🚀 ngrok tunnel started successfully!")
    logger.info(f"📞 Public URL: {public_url}")
    logger.info(f"🌐 Access your bot at: {public_url}/start")
    logger.info(f"💚 Health check: {public_url}/health")
    logger.info("=" * 60)
    
    # Register cleanup function
    atexit.register(cleanup_ngrok)
    
    return public_url


def cleanup_ngrok():
    """Clean up ngrok tunnel on exit."""
    global ngrok_tunnel
    if ngrok_tunnel:
        try:
            ngrok.disconnect(ngrok_tunnel.public_url)
            ngrok.kill()
            logger.info("ngrok tunnel closed")
        except Exception as e:
            logger.error(f"Error closing ngrok tunnel: {e}")


def fix_credentials():
    """
    Fix GOOGLE_VERTEX_CREDENTIALS so Pipecat can parse it.
    Supports both file paths and JSON strings (including quoted JSON strings from .env files).
    """
    creds = GOOGLE_VERTEX_CREDENTIALS
    
    if not creds:
        raise ValueError("GOOGLE_VERTEX_CREDENTIALS environment variable is not set")
    
    # Strip whitespace
    creds = creds.strip()
    
    # Remove surrounding quotes if present (handles .env files that quote the JSON string)
    if (creds.startswith('"') and creds.endswith('"')) or (creds.startswith("'") and creds.endswith("'")):
        creds = creds[1:-1]
    
    # Check if it looks like JSON (starts with { or [)
    if creds.startswith('{') or creds.startswith('['):
        # Try to parse as JSON first
        try:
            creds_dict = json.loads(creds)
            # Ensure proper newline formatting for private_key
            if "private_key" in creds_dict:
                creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
            return json.dumps(creds_dict)
        except json.JSONDecodeError:
            # If JSON parsing fails, continue to check if it's a file path
            pass
    
    # Determine the file path - try multiple locations
    file_path = None
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Check if it's an absolute path
    if os.path.isabs(creds):
        if os.path.isfile(creds):
            file_path = creds
    # Check if it exists as a relative path from current working directory
    elif os.path.isfile(creds):
        file_path = os.path.abspath(creds)
    # Check if it exists relative to the script directory
    else:
        potential_path = os.path.join(script_dir, creds)
        if os.path.isfile(potential_path):
            file_path = potential_path
    
    # If it ends with .json but we haven't found it yet, assume it's a file path
    # and try relative to script directory
    if not file_path and creds.endswith('.json'):
        potential_path = os.path.join(script_dir, creds)
        if os.path.isfile(potential_path):
            file_path = potential_path
    
    # If we found a file path, read from it
    if file_path and os.path.isfile(file_path):
        try:
            with open(file_path, 'r') as f:
                creds_dict = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            raise ValueError(f"Failed to read credentials from file '{file_path}': {e}") from e
    else:
        # Last attempt: try to parse as JSON (might be unquoted JSON string)
        try:
            creds_dict = json.loads(creds)
        except json.JSONDecodeError as e:
            raise ValueError(f"GOOGLE_VERTEX_CREDENTIALS is not valid JSON and not a valid file path. Value: '{creds[:50]}...' Error: {e}") from e
    
    # Ensure proper newline formatting for private_key
    if "private_key" in creds_dict:
        creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

    return json.dumps(creds_dict)


def format_guardrails_for_prompt() -> str:
    """
    Format stored guardrails (question-answer pairs) for inclusion in system prompt.
    Returns an empty string if no guardrails are stored.
    """
    with guardrails_lock:
        if not guardrails_storage:
            return ""
        
        guardrails_text = "\n\n# CUSTOM INSTRUCTIONS AND GUARDRAILS\n\n"
        guardrails_text += "**IMPORTANT: The following question-answer pairs are custom instructions that guide how you should respond to similar questions.**\n\n"
        guardrails_text += "When a user asks a question that is similar to any of the questions below, you MUST respond in the manner specified in the corresponding answer. These instructions override default behavior when applicable.\n\n"
        
        for idx, guardrail in enumerate(guardrails_storage, 1):
            question = guardrail.get("question", "").strip()
            answer = guardrail.get("answer", "").strip()
            
            if question and answer:
                guardrails_text += f"## Instruction {idx}\n\n"
                guardrails_text += f"**When asked (or similar to):** {question}\n\n"
                guardrails_text += f"**You should respond like this:** {answer}\n\n"
        
        guardrails_text += "**Remember:** Use these instructions as a guide. When a user's question is similar to any of the questions above, adapt your response to match the style and content of the corresponding answer, while still being natural and conversational.\n\n"
        guardrails_text += "# END OF CUSTOM INSTRUCTIONS AND GUARDRAILS\n"
        
        return guardrails_text


def get_current_datetime_info():
    """
    Get current date and time information in Asia/Kolkata timezone.
    Returns formatted strings for use in system prompt.
    """
    try:
        # Get current time in Asia/Kolkata timezone
        if ZoneInfo is not None:
            tz = ZoneInfo("Asia/Kolkata")
            now = datetime.now(tz)
            timezone_name = "Asia/Kolkata (IST)"
        else:
            # Fallback to UTC + 5:30 offset manually
            from datetime import timezone
            ist_offset = timedelta(hours=5, minutes=30)
            tz = timezone(ist_offset)
            now = datetime.now(tz)
            timezone_name = "IST (UTC+5:30)"
        
        # Format date as YYYY-MM-DD
        current_date = now.strftime("%Y-%m-%d")
        
        # Format time as HH:MM (24-hour format)
        current_time = now.strftime("%H:%M")
        
        # Get day of week
        day_of_week = now.strftime("%A")
        
        # Get readable date format
        readable_date = now.strftime("%B %d, %Y")  # e.g., "December 31, 2025"
        
        # Calculate tomorrow's date (timedelta is already imported at top of file)
        tomorrow = now + timedelta(days=1)
        tomorrow_date = tomorrow.strftime("%Y-%m-%d")
        tomorrow_readable = tomorrow.strftime("%B %d, %Y")
        tomorrow_day = tomorrow.strftime("%A")
        
        return {
            "current_date": current_date,
            "current_time": current_time,
            "day_of_week": day_of_week,
            "readable_date": readable_date,
            "tomorrow_date": tomorrow_date,
            "tomorrow_readable": tomorrow_readable,
            "tomorrow_day": tomorrow_day,
            "timezone": timezone_name
        }
    except Exception as e:
        logger.error(f"Error getting current datetime: {e}")
        # Fallback to UTC if timezone fails
        now = datetime.now()
        return {
            "current_date": now.strftime("%Y-%m-%d"),
            "current_time": now.strftime("%H:%M"),
            "day_of_week": now.strftime("%A"),
            "readable_date": now.strftime("%B %d, %Y"),
            "tomorrow_date": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
            "tomorrow_readable": (now + timedelta(days=1)).strftime("%B %d, %Y"),
            "tomorrow_day": (now + timedelta(days=1)).strftime("%A"),
            "timezone": "UTC (fallback)"
        }


async def fetch_detailed_information(params: FunctionCallParams):
    """Fetch detailed information from the preprocessor API for queries outside the system prompt"""
    try:
        query = params.arguments["query"]
        phone_number = params.arguments.get("phone_number")
        email = params.arguments.get("email")
        
        # Validate that at least one contact method is provided
        if not phone_number and not email:
            await params.result_callback({
                "summary": "I need either your phone number or email address to send you the information. Could you please provide one of them?",
                "whatsapp_sent": False,
                "email_sent": False
            })
            return
        
        # Prepare request payload - only include provided fields
        request_payload = {"query": query}
        
        if phone_number:
            # Extract exactly 10 digits from the phone number
            # Remove all non-digit characters first
            digits_only = ''.join(filter(str.isdigit, phone_number))
            
            # If the number starts with "91" and has more than 10 digits, remove the "91" prefix
            # to get the actual 10-digit phone number
            if digits_only.startswith("91") and len(digits_only) > 10:
                # Remove the "91" prefix to get the 10-digit number
                phone_number_clean = digits_only[2:]  # Remove first 2 digits (91)
            elif len(digits_only) > 10:
                # If it's longer than 10 digits but doesn't start with 91, take last 10 digits
                phone_number_clean = digits_only[-10:]
            elif len(digits_only) < 10:
                # If less than 10 digits, pad with zeros (shouldn't happen if agent confirmed)
                phone_number_clean = digits_only.zfill(10)
            else:
                # Exactly 10 digits
                phone_number_clean = digits_only
            
            # Ensure we have exactly 10 digits
            if len(phone_number_clean) != 10:
                raise ValueError(f"Phone number must be exactly 10 digits, got {len(phone_number_clean)} digits")
            
            # Add "91" prefix before sending to API
            phone_number_with_prefix = f"91{phone_number_clean}"
            request_payload["number"] = phone_number_with_prefix
            logger.info(f"Phone number provided: {phone_number_with_prefix} (10 digits: {phone_number_clean})")
        
        if email:
            request_payload["email"] = email
            logger.info(f"Email provided: {email}")
        
        logger.info(f"Calling preprocessor API with query: {query}, payload: {list(request_payload.keys())}")
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://vitpreprocessor-739298578243.us-central1.run.app/query",
                json=request_payload
            )
            
            # Log response details for debugging
            logger.info(f"Preprocessor API response status: {response.status_code}")
            
            # If there's an error, log the response body
            if response.status_code != 200:
                try:
                    error_data = response.json()
                    logger.error(f"Preprocessor API error response: {error_data}")
                except:
                    error_text = response.text
                    logger.error(f"Preprocessor API error response (non-JSON): {error_text}")
            
            response.raise_for_status()
            data = response.json()
            
            # Return the summary from the API response
            if data.get("status") == "success":
                summary = data.get("summary", "")
                whatsapp_status = data.get("whatsapp_status", {})
                email_status = data.get("email_status", {})
                whatsapp_sent = whatsapp_status.get("status") == "success"
                email_sent = email_status.get("status") == "success"
                
                logger.info(f"Preprocessor API returned success. Summary length: {len(summary)}")
                logger.info(f"Delivery status - WhatsApp: {whatsapp_status.get('status')}, Email: {email_status.get('status')}")
                
                # Build a clear status message for the agent
                delivery_info = []
                if whatsapp_sent:
                    delivery_info.append("WhatsApp")
                if email_sent:
                    delivery_info.append("email")
                
                if delivery_info:
                    delivery_message = f"Information has been successfully sent via {', '.join(delivery_info)}."
                else:
                    # Check if there were skipped statuses
                    if whatsapp_status.get("status") == "skipped" and email_status.get("status") == "skipped":
                        delivery_message = "I processed your request, but no contact method was available to send the information. Please provide your phone number or email."
                    else:
                        delivery_message = "I've processed your request. The information is being prepared and sent."
                
                await params.result_callback({
                    "summary": f"{summary}\n\n{delivery_message}",
                    "whatsapp_sent": whatsapp_sent,
                    "email_sent": email_sent,
                    "status": "success"
                })
            else:
                # Only return error if status is explicitly not success
                error_message = data.get("error", "Unable to process request at this moment")
                logger.warning(f"Preprocessor API returned non-success status: {data.get('status')}, error: {error_message}")
                await params.result_callback({
                    "summary": f"I'm having trouble processing your request right now. Please try again in a moment.",
                    "whatsapp_sent": False,
                    "email_sent": False,
                    "status": "error",
                    "error": error_message
                })
    except httpx.HTTPStatusError as e:
        # HTTP error from the API
        error_message = f"Server returned status {e.response.status_code}"
        # Try to get detailed error message from response
        try:
            error_detail = e.response.json()
            if isinstance(error_detail, dict) and "detail" in error_detail:
                error_message = f"{error_message}: {error_detail['detail']}"
            logger.error(f"HTTP error fetching detailed information: {error_message}", exc_info=True)
        except:
            try:
                error_text = e.response.text
                logger.error(f"HTTP error fetching detailed information: {error_message}. Response: {error_text}", exc_info=True)
            except:
                logger.error(f"HTTP error fetching detailed information: {error_message}", exc_info=True)
        
        await params.result_callback({
            "summary": f"I'm having trouble processing your request right now. Please try again in a moment.",
            "whatsapp_sent": False,
            "email_sent": False,
            "status": "error",
            "error": error_message
        })
    except httpx.TimeoutException as e:
        # Timeout error
        error_message = "The request took too long to process"
        logger.error(f"Timeout error fetching detailed information: {e}", exc_info=True)
        await params.result_callback({
            "summary": f"I'm having trouble processing your request right now. Please try again in a moment.",
            "whatsapp_sent": False,
            "email_sent": False,
            "status": "error",
            "error": error_message
                })
    except Exception as e:
        # Other exceptions - only report if it's a real error
        error_message = str(e)
        logger.error(f"Error fetching detailed information: {e}", exc_info=True)
        await params.result_callback({
            "summary": f"I'm having trouble processing your request right now. Please try again in a moment.",
            "whatsapp_sent": False,
            "email_sent": False,
            "status": "error",
            "error": error_message
        })


async def get_career_paths(params: FunctionCallParams):
    """Get career paths for a specific branch - internal tool"""
    try:
        branch = params.arguments["branch"]
        branch_lower = branch.lower()
        
        logger.info(f"Getting career paths for branch: {branch}")
        
        # Career paths database
        career_paths_db = {
            "mechanical engineering": {
                "career_paths": [
                    "Design Engineer - Design and develop mechanical systems and components",
                    "Manufacturing Engineer - Optimize production processes and quality control",
                    "Automotive Engineer - Work in automobile design, R&D, and manufacturing",
                    "Aerospace Engineer - Design aircraft, spacecraft, and related systems",
                    "Energy Engineer - Work in renewable energy, power plants, and energy systems",
                    "Project Manager - Lead engineering projects and teams",
                    "Research & Development Engineer - Innovate new products and technologies",
                    "Quality Control Engineer - Ensure product quality and standards",
                    "Maintenance Engineer - Maintain and optimize industrial equipment",
                    "Consultant - Provide expert advice to organizations"
                ]
            },
            "computer science and engineering": {
                "career_paths": [
                    "Software Developer - Build applications and software systems",
                    "Data Scientist - Analyze data and build predictive models",
                    "Machine Learning Engineer - Develop AI and ML solutions",
                    "Full Stack Developer - Work on both frontend and backend systems",
                    "DevOps Engineer - Manage infrastructure and deployment pipelines",
                    "Cybersecurity Analyst - Protect systems from threats and vulnerabilities",
                    "Cloud Architect - Design and manage cloud infrastructure",
                    "Mobile App Developer - Create iOS and Android applications",
                    "Game Developer - Develop video games and interactive experiences",
                    "Technical Lead - Lead development teams and projects"
                ]
            },
            "electronics and communication engineering": {
                "career_paths": [
                    "Embedded Systems Engineer - Design microcontroller-based systems",
                    "VLSI Design Engineer - Design integrated circuits and chips",
                    "Telecommunications Engineer - Work on network infrastructure and communication systems",
                    "RF Engineer - Design radio frequency and wireless systems",
                    "Signal Processing Engineer - Process and analyze signals",
                    "IoT Engineer - Develop Internet of Things solutions",
                    "Hardware Engineer - Design electronic hardware and circuits",
                    "Network Engineer - Design and maintain computer networks",
                    "Research Engineer - Innovate in electronics and communications",
                    "Technical Consultant - Provide expertise to organizations"
                ]
            },
            "electrical and electronics engineering": {
                "career_paths": [
                    "Power Systems Engineer - Design and maintain electrical power systems",
                    "Control Systems Engineer - Design automation and control systems",
                    "Renewable Energy Engineer - Work on solar, wind, and other renewable energy projects",
                    "Electrical Design Engineer - Design electrical systems for buildings and industries",
                    "Instrumentation Engineer - Design measurement and control instruments",
                    "Project Engineer - Manage electrical engineering projects",
                    "Maintenance Engineer - Maintain electrical equipment and systems",
                    "Research Engineer - Innovate in electrical and electronics technologies",
                    "Consultant - Provide electrical engineering expertise",
                    "Entrepreneur - Start your own electrical engineering business"
                ]
            },
            "information technology": {
                "career_paths": [
                    "Software Engineer - Develop software applications and systems",
                    "Network Administrator - Manage and maintain IT networks",
                    "Database Administrator - Manage databases and data systems",
                    "IT Consultant - Provide technology solutions to businesses",
                    "System Administrator - Manage IT infrastructure and servers",
                    "Web Developer - Build websites and web applications",
                    "IT Project Manager - Lead technology projects",
                    "Business Analyst - Bridge business needs and technology solutions",
                    "Cloud Solutions Architect - Design cloud-based solutions",
                    "IT Security Specialist - Protect IT systems and data"
                ]
            }
        }
        
        # Find matching branch (case-insensitive)
        result = None
        for key, value in career_paths_db.items():
            if key in branch_lower or branch_lower in key:
                result = value
                break
        
        if result:
            await params.result_callback({
                "branch": branch,
                "career_paths": result["career_paths"]
            })
        else:
            # Default response if branch not found
            await params.result_callback({
                "branch": branch,
                "career_paths": ["Please contact the counseling office for specific career path information for this branch."]
            })
            
    except Exception as e:
        logger.error(f"Error getting career paths: {e}", exc_info=True)
        await params.result_callback({
            "branch": params.arguments.get("branch", "Unknown"),
            "career_paths": ["I apologize, but I'm having trouble retrieving career paths right now. Please try again."]
        })


async def get_alumni_info(params: FunctionCallParams):
    """Get alumni placement information for a specific branch - internal tool"""
    try:
        branch = params.arguments["branch"]
        branch_lower = branch.lower()
        
        logger.info(f"Getting alumni info for branch: {branch}")
        
        # Alumni information database
        alumni_db = {
            "mechanical engineering": {
                "placement_stats": {
                    "average_package": "₹6.2 LPA",
                    "highest_package": "₹18 LPA",
                    "placement_rate": "92%"
                },
                "top_recruiters": [
                    "Tata Motors", "Mahindra & Mahindra", "L&T", "Caterpillar", "Bosch", "Siemens", "ABB", "Maruti Suzuki"
                ],
                "alumni_highlights": [
                    "Many alumni work in leading automotive companies like Tata Motors and Mahindra",
                    "Strong presence in manufacturing and industrial sectors",
                    "Several alumni have started their own engineering consultancies",
                    "Alumni network actively supports current students through mentorship programs"
                ],
                "external_programs": [
                    "Industry partnerships with major automotive manufacturers for internships",
                    "Collaborative projects with L&T and Siemens",
                    "Guest lectures from industry experts",
                    "Annual industry-academia meet for networking opportunities"
                ]
            },
            "computer science and engineering": {
                "placement_stats": {
                    "average_package": "₹8.5 LPA",
                    "highest_package": "₹28 LPA",
                    "placement_rate": "95%"
                },
                "top_recruiters": [
                    "Amazon", "Microsoft", "Google", "TCS", "Infosys", "Wipro", "Cognizant", "Accenture", "HCL", "Capgemini"
                ],
                "alumni_highlights": [
                    "Alumni working at top tech companies including FAANG",
                    "Strong representation in product-based companies",
                    "Many alumni have founded successful startups",
                    "Active alumni network providing referrals and mentorship"
                ],
                "external_programs": [
                    "Coding bootcamps with industry partners",
                    "Hackathons sponsored by major tech companies",
                    "Summer internship programs with Google, Microsoft, and Amazon",
                    "Industry mentorship program connecting students with alumni"
                ]
            },
            "electronics and communication engineering": {
                "placement_stats": {
                    "average_package": "₹7.1 LPA",
                    "highest_package": "₹22 LPA",
                    "placement_rate": "91%"
                },
                "top_recruiters": [
                    "Qualcomm", "Intel", "Samsung", "Nokia", "Ericsson", "Huawei", "MediaTek", "Broadcom", "Texas Instruments"
                ],
                "alumni_highlights": [
                    "Alumni working in semiconductor and telecommunications industries",
                    "Strong presence in R&D departments of major tech companies",
                    "Several alumni have contributed to 5G and IoT innovations",
                    "Active alumni network in embedded systems and VLSI domains"
                ],
                "external_programs": [
                    "Industry-sponsored research projects with Qualcomm and Intel",
                    "Internship opportunities with leading semiconductor companies",
                    "Technical workshops on latest communication technologies",
                    "Alumni-led mentorship programs for ECE students"
                ]
            },
            "electrical and electronics engineering": {
                "placement_stats": {
                    "average_package": "₹6.8 LPA",
                    "highest_package": "₹20 LPA",
                    "placement_rate": "90%"
                },
                "top_recruiters": [
                    "ABB", "Siemens", "Schneider Electric", "BHEL", "L&T Power", "Adani Power", "Tata Power", "Reliance Energy"
                ],
                "alumni_highlights": [
                    "Alumni working in power generation and distribution companies",
                    "Strong presence in renewable energy sector",
                    "Several alumni have excelled in automation and control systems",
                    "Active alumni network supporting power sector projects"
                ],
                "external_programs": [
                    "Industry partnerships with power companies for field training",
                    "Collaborative projects with ABB and Siemens on smart grid technologies",
                    "Renewable energy workshops and seminars",
                    "Alumni networking events in power and energy sector"
                ]
            },
            "information technology": {
                "placement_stats": {
                    "average_package": "₹7.8 LPA",
                    "highest_package": "₹25 LPA",
                    "placement_rate": "93%"
                },
                "top_recruiters": [
                    "TCS", "Infosys", "Wipro", "Cognizant", "Accenture", "HCL", "Capgemini", "Tech Mahindra", "IBM", "Dell"
                ],
                "alumni_highlights": [
                    "Alumni working across various IT services and consulting companies",
                    "Strong representation in digital transformation projects",
                    "Many alumni have progressed to leadership roles",
                    "Active alumni network providing career guidance"
                ],
                "external_programs": [
                    "Industry-academia partnerships for curriculum development",
                    "Internship programs with major IT service providers",
                    "Technical certification programs in collaboration with industry",
                    "Alumni-led career development workshops"
                ]
            }
        }
        
        # Find matching branch (case-insensitive)
        result = None
        for key, value in alumni_db.items():
            if key in branch_lower or branch_lower in key:
                result = value
                break
        
        if result:
            await params.result_callback({
                "branch": branch,
                "placement_stats": result["placement_stats"],
                "top_recruiters": result["top_recruiters"],
                "alumni_highlights": result["alumni_highlights"],
                "external_programs": result["external_programs"]
            })
        else:
            # Default response if branch not found
            await params.result_callback({
                "branch": branch,
                "placement_stats": {"average_package": "N/A", "highest_package": "N/A", "placement_rate": "N/A"},
                "top_recruiters": [],
                "alumni_highlights": ["Please contact the counseling office for specific alumni information for this branch."],
                "external_programs": []
            })
            
    except Exception as e:
        logger.error(f"Error getting alumni info: {e}", exc_info=True)
        await params.result_callback({
            "branch": params.arguments.get("branch", "Unknown"),
            "placement_stats": {"error": "Unable to retrieve information"},
            "top_recruiters": [],
            "alumni_highlights": ["I apologize, but I'm having trouble retrieving alumni information right now. Please try again."],
            "external_programs": []
        })


def normalize_phone_number(phone_number: str) -> str:
    """Normalize phone number to 10 digits"""
    # Remove all non-digit characters
    digits_only = ''.join(filter(str.isdigit, phone_number))
    
    # If the number starts with "91" and has more than 10 digits, remove the "91" prefix
    if digits_only.startswith("91") and len(digits_only) > 10:
        phone_number_clean = digits_only[2:]  # Remove first 2 digits (91)
    elif len(digits_only) > 10:
        # If it's longer than 10 digits but doesn't start with 91, take last 10 digits
        phone_number_clean = digits_only[-10:]
    elif len(digits_only) < 10:
        # If less than 10 digits, pad with zeros (shouldn't happen if agent confirmed)
        phone_number_clean = digits_only.zfill(10)
    else:
        # Exactly 10 digits
        phone_number_clean = digits_only
    
    return phone_number_clean


def get_mongodb_client():
    """Get MongoDB client connection"""
    if not MONGODB_URI:
        return None
    try:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        # Test the connection
        client.admin.command('ping')
        return client
    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        return None
    except Exception as e:
        logger.error(f"Error connecting to MongoDB: {e}")
        return None


def get_user_data(phone_number: str = None, email: str = None):
    """Check if user exists in MongoDB and retrieve user data and analytics by phone number or email"""
    if not MONGODB_URI:
        logger.warning("MongoDB URI not configured, skipping user lookup")
        return None
    
    if not phone_number and not email:
        logger.warning("Neither phone number nor email provided for user lookup")
        return None
    
    try:
        # Get MongoDB client
        client = get_mongodb_client()
        if not client:
            return None
        
        db = client["VIT"]
        users_collection = db["users"]
        analytics_collection = db["userAnalytics"]
        
        user = None
        
        # Try to find user by phone number first (if provided)
        if phone_number:
            # Normalize phone number to 10 digits
            phone_clean = normalize_phone_number(phone_number)
            
            if len(phone_clean) == 10:
                # Convert to DB format: "91" + 10 digits (e.g., "918438232949")
                phone_db_format = f"91{phone_clean}"
                
                # Find user by phone number - check both formats (DB format and 10-digit format)
                # DB stores as "91" + 10 digits (e.g., "918438232949") in field "phone_number"
                user = users_collection.find_one({"phone_number": phone_db_format})
                
                # If not found, also try 10-digit format (for backward compatibility)
                if not user:
                    user = users_collection.find_one({"phone_number": phone_clean})
                
                # Also try with "phone" field (for backward compatibility)
                if not user:
                    user = users_collection.find_one({"phone": phone_db_format})
                if not user:
                    user = users_collection.find_one({"phone": phone_clean})
        
        # If not found by phone, try email (if provided)
        if not user and email:
            # Normalize email (lowercase, trim)
            email_clean = email.strip().lower()
            user = users_collection.find_one({"email": email_clean})
            
            # Also try case-insensitive search
            if not user:
                user = users_collection.find_one({"email": {"$regex": f"^{email_clean}$", "$options": "i"}})
        
        if not user:
            lookup_info = f"phone: {phone_number}" if phone_number else f"email: {email}"
            logger.info(f"User not found in database for {lookup_info}")
            client.close()
            return None
        
        # Get user ID
        user_id = user.get("_id")
        
        # Find analytics for this user - check "user_id" field (ObjectId) first, then other formats
        analytics = None
        if user_id:
            try:
                from bson import ObjectId
                # Primary: user_id field with ObjectId (as per DB structure)
                analytics = analytics_collection.find_one({"user_id": ObjectId(user_id)})
                
                # Fallback: user_id as string
                if not analytics:
                    analytics = analytics_collection.find_one({"user_id": str(user_id)})
                
                # Fallback: id field with ObjectId
                if not analytics:
                    analytics = analytics_collection.find_one({"id": ObjectId(user_id)})
                
                # Fallback: id field as string
                if not analytics:
                    analytics = analytics_collection.find_one({"id": str(user_id)})
                
                # Fallback: _id field directly
                if not analytics:
                    analytics = analytics_collection.find_one({"_id": user_id})
                
                # Fallback: userId field
                if not analytics:
                    analytics = analytics_collection.find_one({"userId": str(user_id)})
            except Exception as e:
                logger.warning(f"Error querying analytics with ObjectId: {e}")
                # Try string format
                analytics = analytics_collection.find_one({"user_id": str(user_id)})
        
        # Prepare user data
        # Get phone from either "phone_number" or "phone" field
        user_phone = user.get("phone_number") or user.get("phone") or ""
        
        user_data = {
            "exists": True,
            "user_profile": {
                "name": user.get("name", ""),
                "email": user.get("email", ""),
                "phone": user_phone
            },
            "analytics": {
                "course_interest": analytics.get("course_interest", "") or analytics.get("course interest", "") if analytics else "",
                "city": analytics.get("city", "") if analytics else "",
                "budget": analytics.get("budget", "") if analytics else "",
                "hostel_needed": analytics.get("hostel_needed", "") or analytics.get("hostel needed", "") if analytics else "",
                "intent_level": analytics.get("intent_level", "") or analytics.get("intent level", "") if analytics else ""
            } if analytics else {}
        }
        
        client.close()
        lookup_method = f"phone: {user_phone}" if user_phone else f"email: {user_data['user_profile']['email']}"
        logger.info(f"Found existing user: {user_data['user_profile']['name']} ({lookup_method})")
        return user_data
        
    except Exception as e:
        logger.error(f"Error retrieving user data from MongoDB: {e}", exc_info=True)
        return None


async def check_user_exists(params: FunctionCallParams):
    """Check if user exists in database and retrieve their profile and analytics by phone number or email"""
    try:
        phone_number = params.arguments.get("phone_number")
        email = params.arguments.get("email")
        
        # Validate that at least one is provided
        if not phone_number and not email:
            await params.result_callback({
                "user_exists": False,
                "message": "Either phone number or email must be provided to check user existence."
            })
            return
        
        lookup_info = f"phone: {phone_number}" if phone_number else f"email: {email}"
        logger.info(f"Checking user existence for {lookup_info}")
        
        # Get user data from MongoDB
        user_data = get_user_data(phone_number=phone_number, email=email)
        
        if user_data and user_data.get("exists"):
            # User exists - return their data
            await params.result_callback({
                "user_exists": True,
                "user_profile": user_data["user_profile"],
                "analytics": user_data["analytics"],
                "message": f"Found existing user profile for {user_data['user_profile']['name']}. You already have their information including course interest, city, budget, hostel preference, and intent level. Use this information to have a personalized conversation without asking the standard counseling questions."
            })
        else:
            # User doesn't exist
            await params.result_callback({
                "user_exists": False,
                "message": "User not found in database. Proceed with standard counseling flow."
            })
            
    except Exception as e:
        logger.error(f"Error checking user existence: {e}", exc_info=True)
        await params.result_callback({
            "user_exists": False,
            "message": "Error checking user database. Proceeding with standard counseling flow.",
            "error": str(e)
        })


async def create_daily_room() -> tuple[str, str]:
    """Create a Daily room and return the URL and token"""
    async with aiohttp.ClientSession() as session:
        # Create room
        async with session.post(
            "https://api.daily.co/v1/rooms",
            headers={
                "Authorization": f"Bearer {DAILY_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "properties": {
                    "exp": int(time.time()) + 3600,  # 1 hour from now
                    "enable_chat": False,
                    "enable_emoji_reactions": False,
                }
            },
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                logger.error(f"Failed to create room: {response.status} - {error_text}")
                raise Exception(f"Failed to create Daily room: {response.status}")
            
            room_data = await response.json()
            logger.debug(f"Room data: {room_data}")
            
            room_url = room_data.get("url")
            room_name = room_data.get("name")
            
            if not room_url or not room_name:
                logger.error(f"Missing url or name in room response: {room_data}")
                raise Exception("Invalid room data from Daily API")

        # Create token
        async with session.post(
            "https://api.daily.co/v1/meeting-tokens",
            headers={
                "Authorization": f"Bearer {DAILY_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "properties": {
                    "room_name": room_name,
                    "is_owner": True,
                }
            },
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                logger.error(f"Failed to create token: {response.status} - {error_text}")
                raise Exception(f"Failed to create Daily token: {response.status}")
            
            token_data = await response.json()
            logger.debug(f"Token data: {token_data}")
            token = token_data.get("token")
            
            if not token:
                logger.error(f"Missing token in response: {token_data}")
                raise Exception("Invalid token data from Daily API")

    logger.info(f"Successfully created room: {room_url}")
    return room_url, token


async def run_bot(room_url: str, token: str):
    """Run the voice bot in the Daily room"""
    transport = None
    try:
        logger.info(f"Starting bot for room: {room_url}")
        
        # Initialize transport with Silero VAD
        transport = DailyTransport(
            room_url,
            token,
            "Voice Bot",
            DailyParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                video_out_enabled=False,
                vad_analyzer=SileroVADAnalyzer(
                    params=VADParams(
                        stop_secs=0.3,
                        min_volume=0.3,
                    )
                ),
                transcription_enabled=True,
            ),
        )

        # Get project configuration
        project_id = GOOGLE_CLOUD_PROJECT_ID
        location = GOOGLE_CLOUD_LOCATION
        model_id = "gemini-live-2.5-flash-preview-native-audio-09-2025"
        
        # Build the full model path
        model_path = f"projects/{project_id}/locations/{location}/publishers/google/models/{model_id}"
        
        logger.info(f"Using Vertex AI model: {model_path}")

        # Get temperature from environment or use default (lower temperature for better instruction following)
        temperature = float(os.getenv("LLM_TEMPERATURE", "0.3"))
        logger.info(f"Using LLM temperature: {temperature}")

        # Get current date and time information
        datetime_info = get_current_datetime_info()
        logger.info(f"Current date/time context: {datetime_info['current_date']} {datetime_info['current_time']} ({datetime_info['timezone']})")
        
        # Inject current date/time into system prompt
        datetime_context = f"""

## CURRENT DATE AND TIME INFORMATION

**IMPORTANT: Use this information when answering questions about dates/times.**

- **Current Date**: {datetime_info['readable_date']} ({datetime_info['day_of_week']})
- **Current Date (YYYY-MM-DD format)**: {datetime_info['current_date']}
- **Current Time**: {datetime_info['current_time']} ({datetime_info['timezone']})
- **Tomorrow's Date**: {datetime_info['tomorrow_readable']} ({datetime_info['tomorrow_day']})
- **Tomorrow's Date (YYYY-MM-DD format)**: {datetime_info['tomorrow_date']}
- Current timezone is {datetime_info['timezone']}

"""
        
        # Get guardrails and format them for the prompt
        guardrails_context = format_guardrails_for_prompt()
        
        # Combine system prompt with datetime context and guardrails
        system_instruction = SYSTEM_PROMPT + datetime_context + guardrails_context

        # Define the detailed information tool
        detailed_info_function = FunctionSchema(
            name="get_detailed_information",
            description="Send course-specific brochures and detailed information to the student via WhatsApp and/or email. IMPORTANT: This tool processes requests in the background. The tool will return a status indicating success or error. Only report errors to the student if the tool explicitly returns an error status. The tool automatically sends WhatsApp message (if phone number provided) and/or email (if email provided) to the student. At least one of phone_number or email must be provided. The tool response will indicate what was successfully sent (WhatsApp, email, or both).",
            properties={
                "query": {
                    "type": "string",
                    "description": "The specific course or branch information the student wants (e.g., 'Computer Science and Engineering brochure', 'Mechanical Engineering course details', 'Hostel facilities details')",
                },
                "phone_number": {
                    "type": "string",
                    "description": "The student's phone number (10 digits, will be prefixed with +91 automatically). Optional - provide if available.",
                },
                "email": {
                    "type": "string",
                    "description": "The student's email address. Optional - provide if available.",
                },
            },
            required=["query"],
        )

        # Define the career paths tool
        career_paths_function = FunctionSchema(
            name="get_career_paths",
            description="Get career paths for a specific branch. Use this tool when the student asks about career options, job prospects, or what they can do after completing a particular branch. This is an internal tool that provides accurate career path information based on the branch name. Always use this tool instead of making up career paths.",
            properties={
                "branch": {
                    "type": "string",
                    "description": "The exact branch name (e.g., 'Computer Science and Engineering', 'Mechanical Engineering', 'Electronics and Communication Engineering', 'Electrical and Electronics Engineering', 'Information Technology')",
                },
            },
            required=["branch"],
        )

        # Define the alumni information tool
        alumni_info_function = FunctionSchema(
            name="get_alumni_info",
            description="Get alumni placement information and external program details for a specific branch. Use this tool when the student asks about placements, alumni success stories, or external programs for a particular branch. This is an internal tool that provides accurate branch-specific alumni and placement information.",
            properties={
                "branch": {
                    "type": "string",
                    "description": "The exact branch name (e.g., 'Computer Science and Engineering', 'Mechanical Engineering', 'Electronics and Communication Engineering', 'Electrical and Electronics Engineering', 'Information Technology')",
                },
            },
            required=["branch"],
        )

        # Define the user check tool
        check_user_function = FunctionSchema(
            name="check_user_exists",
            description="CRITICAL: After collecting the student's name and at least one contact method (phone number OR email), you MUST call this tool to check if the student already exists in the database. This tool checks the database by phone number or email. If the user exists, it returns their profile (name, email, phone) and analytics (course interest, city, budget, hostel needed, intent level). If user exists, you should skip the standard counseling questions (marks, stream, interests) and use the existing information to have a personalized conversation. If user doesn't exist, proceed with the standard counseling flow. At least one of phone_number or email must be provided.",
            properties={
                "phone_number": {
                    "type": "string",
                    "description": "The student's phone number (10 digits) that you just collected and confirmed. Optional - provide if available.",
                },
                "email": {
                    "type": "string",
                    "description": "The student's email address. Optional - provide if available.",
                },
            },
            required=[],
        )

        tools = ToolsSchema(standard_tools=[detailed_info_function, career_paths_function, alumni_info_function, check_user_function])

        # Initialize Vertex AI LLM Service with tools
        llm = GeminiLiveVertexLLMService(
            credentials=fix_credentials(),
            project_id=project_id,
            location=location,
            model=model_path,
            system_instruction=system_instruction,
            voice_id="Aoede",  # Options: Aoede, Charon, Fenrir, Kore, Puck
            temperature=temperature,
            tools=tools,
        )

        # Register the functions
        llm.register_function("get_detailed_information", fetch_detailed_information)
        llm.register_function("get_career_paths", get_career_paths)
        llm.register_function("get_alumni_info", get_alumni_info)
        llm.register_function("check_user_exists", check_user_exists)

        # Create context with initial greeting and user information collection
        context = LLMContext(
            [
                {
                    "role": "user",
                    "content": "Greet the student warmly and introduce yourself as Natalie, a college counselor for VIT. Then ask for their name. Once you have their name, ask for their mobile number (10 digits) OR email address - at least one of them is required. If they provide a phone number, confirm it by reciting the 10 digits back to them. You can ask for both, but at least one contact method is mandatory. Once you have their name and at least one contact method (phone or email), you can proceed with the counseling session. Be friendly, warm, and approachable - like a caring counselor. Keep each question brief and wait for their response before moving to the next question."
                }
            ]
        )

        # Use context aggregator for proper conversation flow
        context_aggregator = LLMContextAggregatorPair(context)

        # Build pipeline with context aggregator
        pipeline = Pipeline(
            [
                transport.input(),
                context_aggregator.user(),
                llm,
                transport.output(),
                context_aggregator.assistant(),
            ]
        )
        
        # Store user_id for conversation history
        user_id_for_history = None

        # Create task
        task = PipelineTask(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=16000,
                audio_out_sample_rate=16000,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
        )

        # Set up event handlers
        @transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(transport, participant):
            logger.info(f"First participant joined: {participant}")
            # Start capturing transcription for the participant
            await transport.capture_participant_transcription(participant["id"])
            
            # Give a moment for audio to be ready, then start conversation
            await asyncio.sleep(0.5)
            try:
                # Use LLMRunFrame to immediately trigger the LLM with the initial context
                await task.queue_frames([LLMRunFrame()])
                logger.info("Initial greeting triggered with LLMRunFrame")
            except Exception as e:
                logger.error(f"Error sending greeting: {e}")

        @transport.event_handler("on_participant_left")
        async def on_participant_left(transport, participant, reason):
            logger.info(f"Participant left: {participant}, reason: {reason}")
            
            # Get conversation history from context
            try:
                conversation_history = []
                
                # Access context messages from the aggregator
                # Try multiple ways to access the messages
                context_messages = []
                if hasattr(context_aggregator, 'context') and hasattr(context_aggregator.context, 'messages'):
                    context_messages = context_aggregator.context.messages
                elif hasattr(context_aggregator, '_context') and hasattr(context_aggregator._context, 'messages'):
                    context_messages = context_aggregator._context.messages
                elif hasattr(context, 'messages'):
                    context_messages = context.messages
                else:
                    # Try to get from aggregator's user/assistant processors
                    try:
                        if hasattr(context_aggregator, 'user') and hasattr(context_aggregator.user, '_context'):
                            context_messages = context_aggregator.user._context.messages if hasattr(context_aggregator.user._context, 'messages') else []
                    except:
                        pass
                
                # Build conversation history string
                for msg in context_messages:
                    role = msg.get("role", "unknown")
                    content = msg.get("content", "")
                    # Skip system messages and empty content
                    if content and role in ["user", "assistant"]:
                        speaker = "User" if role == "user" else "Natalie (Agent)"
                        conversation_history.append(f"{speaker}: {content}")
                
                conversation_text = "\n".join(conversation_history)
                
                if conversation_text.strip():
                    logger.info(f"Sending conversation history to postprocessor (length: {len(conversation_text)} chars, {len(conversation_history)} messages)...")
                    # Send to postprocessor
                    try:
                        async with httpx.AsyncClient(timeout=30.0) as client:
                            response = await client.post(
                                "https://vitpostprocessor-739298578243.us-central1.run.app/process",
                                json={"conversation": conversation_text}
                            )
                            if response.status_code == 200:
                                logger.info("Conversation history sent to postprocessor successfully")
                            else:
                                logger.warning(f"Postprocessor returned status {response.status_code}: {response.text}")
                    except Exception as e:
                        logger.error(f"Error sending conversation history to postprocessor: {e}", exc_info=True)
                else:
                    logger.warning("No conversation history to send - context messages not accessible")
                    
            except Exception as e:
                logger.error(f"Error processing conversation history: {e}", exc_info=True)
            
            await task.queue_frame(EndFrame())

        @transport.event_handler("on_participant_joined")
        async def on_participant_joined(transport, participant):
            logger.info(f"Participant joined: {participant}")
            # Capture transcription for any new participant
            await transport.capture_participant_transcription(participant["id"])

        logger.info("Starting pipeline runner")
        runner = PipelineRunner()
        await runner.run(task)
        
        logger.info("Pipeline runner completed")

    except Exception as e:
        logger.error(f"Error running bot: {e}", exc_info=True)
        raise
    finally:
        if transport:
            try:
                logger.info("Cleaning up transport")
                await transport.cleanup()
            except Exception as e:
                logger.error(f"Error cleaning up transport: {e}")


@app.post("/start")
async def start_session(request: Request):
    """Create a Daily room, start the bot, and return connection details"""
    try:
        logger.info("Creating Daily room and starting bot...")
        
        # Create room and token
        room_url, token = await create_daily_room()
        logger.info(f"Created room: {room_url}")

        # Start bot in background
        asyncio.create_task(run_bot(room_url, token))

        # Return connection details
        return JSONResponse(
            content={
                "room_url": room_url,
                "token": token,
            }
        )

    except Exception as e:
        logger.error(f"Error starting session: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": str(e)},
        )


# Pydantic models for request validation
class GuardrailItem(BaseModel):
    question: str
    answer: str


class GuardrailsRequest(BaseModel):
    guardrails: List[GuardrailItem]


@app.post("/upload-guardrails")
async def upload_guardrails(request: GuardrailsRequest):
    """
    Upload question-answer pairs (guardrails/instructions) that will guide the agent's responses.
    
    Request body should be:
    {
        "guardrails": [
            {
                "question": "What is your refund policy?",
                "answer": "We offer a full refund within 30 days of enrollment..."
            },
            {
                "question": "How do I apply?",
                "answer": "You can apply online through our website..."
            }
        ]
    }
    
    These question-answer pairs will be included in the system prompt for all future conversations.
    When a user asks a question similar to any uploaded question, the agent will respond in the manner specified.
    """
    try:
        if not request.guardrails:
            raise HTTPException(status_code=400, detail="At least one guardrail (question-answer pair) is required")
        
        # Validate guardrails
        validated_guardrails = []
        for idx, guardrail in enumerate(request.guardrails):
            question = guardrail.question.strip()
            answer = guardrail.answer.strip()
            
            if not question:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Guardrail at index {idx} has an empty question"
                )
            if not answer:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Guardrail at index {idx} has an empty answer"
                )
            
            validated_guardrails.append({
                "question": question,
                "answer": answer
            })
        
        # Store guardrails (replace existing ones)
        with guardrails_lock:
            guardrails_storage.clear()
            guardrails_storage.extend(validated_guardrails)
        
        logger.info(f"Successfully uploaded {len(validated_guardrails)} guardrail(s)")
        
        return JSONResponse(
            content={
                "status": "success",
                "message": f"Successfully uploaded {len(validated_guardrails)} guardrail(s)",
                "count": len(validated_guardrails)
            }
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading guardrails: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.get("/guardrails")
async def get_guardrails():
    """
    Get all currently stored guardrails (question-answer pairs).
    Returns guardrails with their indices for easy deletion.
    """
    with guardrails_lock:
        # Include index with each guardrail for easier deletion
        guardrails_with_index = [
            {
                "index": idx,
                "question": guardrail.get("question", ""),
                "answer": guardrail.get("answer", "")
            }
            for idx, guardrail in enumerate(guardrails_storage)
        ]
        
        return JSONResponse(
            content={
                "status": "success",
                "guardrails": guardrails_with_index,
                "count": len(guardrails_storage)
            }
        )


@app.delete("/guardrails/{index}")
async def delete_guardrail_by_index(index: int):
    """
    Delete a specific guardrail by its index (0-based).
    
    Use GET /guardrails first to see the list of guardrails and their indices.
    """
    try:
        with guardrails_lock:
            if index < 0 or index >= len(guardrails_storage):
                raise HTTPException(
                    status_code=404,
                    detail=f"Guardrail at index {index} not found. There are {len(guardrails_storage)} guardrail(s) (indices 0-{len(guardrails_storage)-1 if guardrails_storage else 0})."
                )
            
            deleted_guardrail = guardrails_storage.pop(index)
            logger.info(f"Deleted guardrail at index {index}: {deleted_guardrail.get('question', '')[:50]}...")
        
        return JSONResponse(
            content={
                "status": "success",
                "message": f"Successfully deleted guardrail at index {index}",
                "deleted_guardrail": deleted_guardrail
            }
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting guardrail: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


class DeleteGuardrailRequest(BaseModel):
    question: str


@app.post("/guardrails/delete")
async def delete_guardrail_by_question(request: DeleteGuardrailRequest):
    """
    Delete a guardrail by matching the question text (case-insensitive, partial match supported).
    
    Request body:
    {
        "question": "What is your refund policy?"
    }
    
    This will find and delete the first guardrail whose question matches (case-insensitive).
    """
    try:
        question_to_delete = request.question.strip()
        if not question_to_delete:
            raise HTTPException(status_code=400, detail="Question cannot be empty")
        
        with guardrails_lock:
            deleted_index = None
            deleted_guardrail = None
            
            # Find the guardrail by matching question (case-insensitive)
            for idx, guardrail in enumerate(guardrails_storage):
                if guardrail.get("question", "").strip().lower() == question_to_delete.lower():
                    deleted_index = idx
                    deleted_guardrail = guardrails_storage.pop(idx)
                    break
            
            if deleted_index is None:
                # Try partial match if exact match not found
                for idx, guardrail in enumerate(guardrails_storage):
                    guardrail_question = guardrail.get("question", "").strip().lower()
                    if question_to_delete.lower() in guardrail_question or guardrail_question in question_to_delete.lower():
                        deleted_index = idx
                        deleted_guardrail = guardrails_storage.pop(idx)
                        break
            
            if deleted_index is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"No guardrail found matching question: '{request.question}'. Use GET /guardrails to see all available guardrails."
                )
            
            logger.info(f"Deleted guardrail at index {deleted_index} matching question: {question_to_delete[:50]}...")
        
        return JSONResponse(
            content={
                "status": "success",
                "message": f"Successfully deleted guardrail matching question",
                "deleted_guardrail": deleted_guardrail,
                "deleted_index": deleted_index
            }
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting guardrail by question: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.delete("/guardrails")
async def clear_guardrails():
    """
    Clear all stored guardrails.
    """
    with guardrails_lock:
        count = len(guardrails_storage)
        guardrails_storage.clear()
    
    logger.info(f"Cleared {count} guardrail(s)")
    
    return JSONResponse(
        content={
            "status": "success",
            "message": f"Cleared {count} guardrail(s)"
        }
    )


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("PORT", "8001"))
    
    # Start ngrok tunnel before starting the server
    try:
        public_url = start_ngrok_tunnel(port)
        logger.info("🎉 Bot is ready to accept connections!")
        logger.info(f"📋 Make POST requests to: {public_url}/start")
    except Exception as e:
        logger.error(f"Failed to start ngrok tunnel: {e}")
        logger.warning("⚠️  Continuing without ngrok. Bot will only be accessible locally.")
    
    # Start the FastAPI server
    uvicorn.run(app, host="0.0.0.0", port=port)

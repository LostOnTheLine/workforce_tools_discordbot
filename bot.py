import discord
from discord.ext import commands
import pytesseract
from PIL import Image, ImageEnhance
import io
from googleapiclient.discovery import build
from google.oauth2 import service_account
import re
from datetime import datetime, timedelta
import os
import logging
import traceback
import shutil

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Environment variables
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
CALENDAR_ID = os.getenv('CALENDAR_ID', 'primary')  # Default to 'primary' if not set

# Google Calendar setup with Service Account
creds_path = '/creds/service-account-key.json'
if not os.path.exists(creds_path):
    logger.error("Service account key file not found! Please mount /creds with service-account-key.json")
    exit(1)

SCOPES = ['https://www.googleapis.com/auth/calendar']
creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
service = build('calendar', 'v3', credentials=creds)

# Directory for saving OCR results
OCR_DIR = '/data'
if not os.path.exists(OCR_DIR):
    os.makedirs(OCR_DIR)

def save_ocr_result(text):
    """Save the OCR result to a file and rotate the last 3 results."""
    try:
        # Delete the oldest file (ocr_result_3.txt) if it exists
        if os.path.exists(os.path.join(OCR_DIR, 'ocr_result_3.txt')):
            os.remove(os.path.join(OCR_DIR, 'ocr_result_3.txt'))
        # Move ocr_result_2.txt to ocr_result_3.txt
        if os.path.exists(os.path.join(OCR_DIR, 'ocr_result_2.txt')):
            shutil.move(os.path.join(OCR_DIR, 'ocr_result_2.txt'), os.path.join(OCR_DIR, 'ocr_result_3.txt'))
        # Move ocr_result_1.txt to ocr_result_2.txt
        if os.path.exists(os.path.join(OCR_DIR, 'ocr_result_1.txt')):
            shutil.move(os.path.join(OCR_DIR, 'ocr_result_1.txt'), os.path.join(OCR_DIR, 'ocr_result_2.txt'))
        # Save the new result as ocr_result_1.txt
        with open(os.path.join(OCR_DIR, 'ocr_result_1.txt'), 'w') as f:
            f.write(text)
        logger.info("Saved OCR result to /data/ocr_result_1.txt")
    except Exception as e:
        logger.error(f"Failed to save OCR result: {str(e)}")

def preprocess_image(image):
    """Preprocess the image to improve OCR accuracy."""
    # Convert to grayscale
    image = image.convert('L')
    # Enhance contrast
    enhancer = ImageEnhance.Contrast(image)
    image = enhancer.enhance(2.0)  # Increase contrast (adjust as needed)
    return image

@bot.event
async def on_ready():
    logger.info(f'Bot is ready as {bot.user}')
    # Send a startup message to the #work-calendar channel
    for guild in bot.guilds:
        channel = discord.utils.get(guild.text_channels, name='work-calendar')
        if channel:
            await channel.send("Bot is now active and ready to process screenshots!")
            break
    else:
        logger.warning("Could not find #work-calendar channel to send startup message.")

@bot.event
async def on_message(message):
    logger.info(f"Received message from {message.author} in channel {message.channel.name}")
    if message.author == bot.user:
        logger.info("Message is from the bot itself, ignoring.")
        return
    if not message.attachments:
        logger.info("Message has no attachments, ignoring.")
        return
    if message.channel.name != 'work-calendar':
        logger.info(f"Message is not in #work-calendar (channel: {message.channel.name}), ignoring.")
        return

    logger.info(f"Processing message with {len(message.attachments)} attachments")
    for attachment in message.attachments:
        if attachment.filename.endswith(('.png', '.jpg', '.jpeg')):
            logger.info(f"Found image attachment: {attachment.filename}")
            try:
                # Download image
                image_data = await attachment.read()
                image = Image.open(io.BytesIO(image_data))
                
                # Preprocess the image for better OCR
                image = preprocess_image(image)
                
                # OCR
                logger.info("Performing OCR on image")
                try:
                    text = pytesseract.image_to_string(image)
                except Exception as e:
                    logger.error(f"OCR error: {str(e)}")
                    await message.channel.send("Error: Failed to perform OCR on the image. Please try a different image or ensure the text is clear.")
                    return
                logger.info(f"OCR result:\n{text}")
                
                # Save OCR result to file
                save_ocr_result(text)
                
                # Current date for reference, normalized to midnight
                current_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                date_range_start = current_date - timedelta(days=7)  # 7 days in the past
                date_range_end = current_date + timedelta(days=40)   # 40 days in the future
                
                # Parse events
                events = []
                event_details = []  # To store details for the confirmation message
                lines = text.split('\n')
                i = 0
                while i < len(lines):
                    line = lines[i].strip()
                    
                    # Skip empty lines or irrelevant lines
                    if not line or 'Associate' in line or 'Schedule' in line or 'hours' in line:
                        i += 1
                        continue
                    
                    # Check if this line starts a new day block (ends with '>')
                    if line.endswith('>') or line.endswith('>?'):
                        # Check if this is a day without a shift (e.g., "Fri >")
                        no_shift_match = re.match(r'^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s*>$', line)
                        if no_shift_match:
                            day_of_week = no_shift_match.group(1)
                            logger.info(f"Found day without shift: {day_of_week}")
                            i += 1
                            # Skip the next line (should be the date number)
                            if i < len(lines) and re.match(r'^\d{1,2}$', lines[i].strip()):
                                i += 1
                            continue
                        
                        # Parse the day of the week and shift time from the first line
                        day_shift_match = re.match(r'^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2}:\d{2}\s+[AP]M)\s*-\s*(\d{1,2}:\d{2}\s+[AP]M)\s*\[\d{1,2}:\d{2}\]\s*.*>[?]?$', line)
                        if not day_shift_match:
                            logger.warning(f"Line does not match expected day shift format: {line}")
                            i += 1
                            continue
                        
                        day_of_week, start_time, end_time = day_shift_match.groups()
                        logger.info(f"Matched day shift: {day_of_week}, {start_time} - {end_time}")
                        
                        # Get the date number and event title from the next non-empty line
                        i += 1
                        while i < len(lines):
                            next_line = lines[i].strip()
                            if not next_line or 'Associate' in next_line:
                                i += 1
                                continue
                            date_title_match = re.match(r'^(\d{1,2})\s+(.+)$', next_line)
                            if not date_title_match:
                                logger.warning(f"Line does not match expected date/title format: {next_line}")
                                i += 1
                                continue
                            break
                        else:
                            logger.warning("Reached end of lines while looking for date/title")
                            break
                        
                        day_num, event_title = date_title_match.groups()
                        day_num = int(day_num)
                        logger.info(f"Parsed date number and title: {day_num}, {event_title}")
                        
                        # Find the correct month and year for this date
                        event_date = None
                        for month_offset in range(0, 3):  # Check current month and next two months
                            test_date = datetime.now().replace(day=1, month=datetime.now().month + month_offset, hour=0, minute=0, second=0, microsecond=0)
                            if test_date.month > 12:
                                test_date = test_date.replace(year=test_date.year + 1, month=1)
                            try:
                                test_date = test_date.replace(day=day_num)
                                # Check if the day of the week matches and the date is within the allowed range
                                if test_date.strftime('%a')[:3] == day_of_week and date_range_start <= test_date <= date_range_end:
                                    event_date = test_date
                                    logger.info(f"Parsed date: {event_date.strftime('%Y-%m-%d')} ({day_of_week})")
                                    break
                            except ValueError:
                                continue
                        
                        if not event_date:
                            logger.warning(f"Could not determine date for {day_of_week} {day_num}")
                            i += 1
                            continue
                        
                        # Create the event for this day
                        start_dt = datetime.strptime(f"{event_date.strftime('%Y-%m-%d')} {start_time}", '%Y-%m-%d %I:%M %p')
                        end_dt = datetime.strptime(f"{event_date.strftime('%Y-%m-%d')} {end_time}", '%Y-%m-%d %I:%M %p')
                        logger.info(f"Parsed shift: {start_dt} to {end_dt}, Title: {event_title}")
                        
                        # Check for existing events on this day
                        day_start = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
                        day_end = day_start + timedelta(days=1)
                        try:
                            existing_events = service.events().list(
                                calendarId=CALENDAR_ID,
                                timeMin=day_start.isoformat() + 'Z',
                                timeMax=day_end.isoformat() + 'Z',
                                singleEvents=True
                            ).execute().get('items', [])
                        except Exception as e:
                            logger.error(f"Google Calendar API error (list events): {str(e)}")
                            await message.channel.send("Error: Failed to access Google Calendar (list events). Please check the Service Account permissions.")
                            return
                        
                        # Delete existing events for this day
                        for event in existing_events:
                            try:
                                service.events().delete(calendarId=CALENDAR_ID, eventId=event['id']).execute()
                                logger.info(f"Deleted existing event on {day_start.strftime('%Y-%m-%d')}: {event['summary']}")
                            except Exception as e:
                                logger.error(f"Google Calendar API error (delete event): {str(e)}")
                                await message.channel.send("Error: Failed to delete existing events in Google Calendar. Please check the Service Account permissions.")
                                return
                        
                        # Create the event
                        event = {
                            'summary': event_title,
                            'start': {
                                'dateTime': start_dt.isoformat(),
                                'timeZone': 'America/Phoenix'
                            },
                            'end': {
                                'dateTime': end_dt.isoformat(),
                                'timeZone': 'America/Phoenix'
                            }
                        }
                        events.append((event, start_dt, end_dt))  # Store event with start and end times for the confirmation message
                        
                        # Move to the next line and continue looking for the next day block
                        i += 1
                        continue
                    
                    i += 1
                
                # Add events to Google Calendar and collect details for the confirmation message
                for event, start_dt, end_dt in events:
                    try:
                        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
                        logger.info(f"Added event: {event['summary']} on {event['start']['dateTime']}")
                        # Format the event details for the confirmation message
                        event_date_str = start_dt.strftime('%Y/%m/%d')
                        start_time_str = start_dt.strftime('%I:%M%p').lstrip('0')
                        end_time_str = end_dt.strftime('%I:%M%p').lstrip('0')
                        event_details.append(f"{event_date_str}: {start_time_str} - {end_time_str}")
                    except Exception as e:
                        logger.error(f"Google Calendar API error (insert event): {str(e)}")
                        await message.channel.send("Error: Failed to add events to Google Calendar. Please check the Service Account permissions.")
                        return
                
                # Send the confirmation message with event details
                confirmation_message = f"Added {len(events)} events to your Google Calendar!"
                if event_details:
                    confirmation_message += "\n" + "\n".join(event_details)
                await message.channel.send(confirmation_message)
                logger.info(f"Successfully added {len(events)} events to Google Calendar")
            except Exception as e:
                logger.error(f"Unexpected error: {traceback.format_exc()}")
                await message.channel.send(f"Unexpected error occurred: {str(e)}. Please try again or contact the bot owner.")

    await bot.process_commands(message)

if not DISCORD_BOT_TOKEN:
    logger.error("DISCORD_BOT_TOKEN environment variable not set!")
    exit(1)

bot.run(DISCORD_BOT_TOKEN)
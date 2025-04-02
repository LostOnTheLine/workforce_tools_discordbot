import discord
from discord.ext import commands
import pytesseract
from PIL import Image
import io
from googleapiclient.discovery import build
from google.oauth2 import service_account
import re
from datetime import datetime, timedelta
import os
import traceback

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
    print("Service account key file not found! Please mount /creds with service-account-key.json")
    exit(1)

SCOPES = ['https://www.googleapis.com/auth/calendar']
creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
service = build('calendar', 'v3', credentials=creds)

@bot.event
async def on_ready():
    print(f'Bot is ready as {bot.user}')
    # Send a startup message to the #work-calendar channel
    for guild in bot.guilds:
        channel = discord.utils.get(guild.text_channels, name='work-calendar')
        if channel:
            await channel.send("Bot is now active and ready to process screenshots!")
            break
    else:
        print("Could not find #work-calendar channel to send startup message.")

@bot.event
async def on_message(message):
    print(f"Received message from {message.author} in channel {message.channel.name}")
    if message.author == bot.user:
        print("Message is from the bot itself, ignoring.")
        return
    if not message.attachments:
        print("Message has no attachments, ignoring.")
        return
    if message.channel.name != 'work-calendar':
        print(f"Message is not in #work-calendar (channel: {message.channel.name}), ignoring.")
        return

    print(f"Processing message with {len(message.attachments)} attachments")
    for attachment in message.attachments:
        if attachment.filename.endswith(('.png', '.jpg', '.jpeg')):
            print(f"Found image attachment: {attachment.filename}")
            try:
                # Download image
                image_data = await attachment.read()
                image = Image.open(io.BytesIO(image_data))
                
                # OCR
                print("Performing OCR on image")
                try:
                    text = pytesseract.image_to_string(image)
                except Exception as e:
                    print(f"OCR error: {str(e)}")
                    await message.channel.send("Error: Failed to perform OCR on the image. Please try a different image or ensure the text is clear.")
                    return
                print(f"OCR result: {text}")
                
                # Current date for reference
                current_date = datetime.now()
                two_months_later = current_date + timedelta(days=60)
                
                # Parse events
                events = []
                lines = text.split('\n')
                current_day = None
                current_date = datetime.now()  # Initialize with current date instead of None
                # Removed current_month since it's not used correctly

                for line in lines:
                    line = line.strip()
                    # Match day and date (e.g., "Mon 14")
                    day_match = re.match(r'^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2})$', line)
                    if day_match:
                        current_day, day_num = day_match.groups()
                        day_num = int(day_num)
                        
                        # Find the correct month and year for this date
                        for month_offset in range(0, 3):  # Check current month and next two months
                            test_date = current_date.replace(day=1, month=current_date.month + month_offset)
                            if test_date.month > 12:
                                test_date = test_date.replace(year=test_date.year + 1, month=1)
                            try:
                                test_date = test_date.replace(day=day_num)
                                # Check if the day of the week matches
                                if test_date.strftime('%a')[:3] == current_day and current_date <= test_date <= two_months_later:
                                    current_date = test_date
                                    break
                            except ValueError:
                                continue
                        continue
                    
                    # Match shift time and event (e.g., "10:00 AM - 7:00 PM [8:00]")
                    shift_match = re.match(r'(\d{1,2}:\d{2}\s+[AP]M)\s*-\s*(\d{1,2}:\d{2}\s+[AP]M)\s*\[\d{1,2}:\d{2}\]', line)
                    if shift_match and current_date:
                        start_time, end_time = shift_match.groups()
                        
                        # Get the event title (next line after the shift time)
                        event_title = lines[lines.index(line) + 1].strip()
                        if not event_title or 'Associate' not in event_title:
                            event_title = lines[lines.index(line) + 2].strip()
                        
                        # Parse start and end times
                        start_dt = datetime.strptime(f"{current_date.strftime('%Y-%m-%d')} {start_time}", '%Y-%m-%d %I:%M %p')
                        end_dt = datetime.strptime(f"{current_date.strftime('%Y-%m-%d')} {end_time}", '%Y-%m-%d %I:%M %p')
                        
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
                            print(f"Google Calendar API error (list events): {str(e)}")
                            await message.channel.send("Error: Failed to access Google Calendar (list events). Please check the Service Account permissions.")
                            return
                        
                        # Delete existing events for this day
                        for event in existing_events:
                            try:
                                service.events().delete(calendarId=CALENDAR_ID, eventId=event['id']).execute()
                                print(f"Deleted existing event on {day_start.strftime('%Y-%m-%d')}: {event['summary']}")
                            except Exception as e:
                                print(f"Google Calendar API error (delete event): {str(e)}")
                                await message.channel.send("Error: Failed to delete existing events in Google Calendar. Please check the Service Account permissions.")
                                return
                        
                        # Create new event
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
                        events.append(event)
                
                # Add events to Google Calendar
                for event in events:
                    try:
                        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
                    except Exception as e:
                        print(f"Google Calendar API error (insert event): {str(e)}")
                        await message.channel.send("Error: Failed to add events to Google Calendar. Please check the Service Account permissions.")
                        return
                
                await message.channel.send(f'Added {len(events)} events to your Google Calendar!')
            except Exception as e:
                print(f"Unexpected error: {traceback.format_exc()}")
                await message.channel.send(f"Unexpected error occurred: {str(e)}. Please try again or contact the bot owner.")

    await bot.process_commands(message)

bot.run(DISCORD_BOT_TOKEN)
import asyncio
import json
import os
import time
from playwright.async_api import async_playwright, Page

# --- Configuration ---
VERTEX_URL = "https://console.cloud.google.com/vertex-ai/studio/multimodal?mode=prompt&model=gemini-2.5-flash-lite-preview-09-2025"
COOKIES_ENV_VAR = "GOOGLE_COOKIES"

class CloudHarvester:
    def __init__(self, cred_manager):
        self.cred_manager = cred_manager
        self.browser = None
        self.page = None
        self.is_running = False
        self.last_harvest_time = 0
        self.current_cookies = os.environ.get(COOKIES_ENV_VAR)
        self.restart_requested = False

    async def update_cookies(self, new_cookies_json):
        """Updates cookies and triggers a browser restart."""
        print("🍪 Cloud Harvester: Received new cookies. Scheduling restart...")
        self.current_cookies = new_cookies_json
        self.restart_requested = True

    async def start(self):
        """Starts the browser and the harvesting loop."""
        if self.is_running:
            return
        
        if not self.current_cookies:
            print("⚠️ Cloud Harvester: No cookies available. Waiting for update via /admin...")
            # Wait loop for cookies
            # while not self.current_cookies:
            #     await asyncio.sleep(5)
            # Allow proceeding without cookies based on user feedback (experimental)
            print("⚠️ Cloud Harvester: Proceeding without cookies (Experimental).")
        
        print("☁️ Cloud Harvester: Starting...")
        self.is_running = True
        
        while self.is_running:
            try:
                async with async_playwright() as p:
                    # Launch browser (headless=True for cloud)
                    self.browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
                    context = await self.browser.new_context(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    )
                    
                    # Load Cookies
                    if self.current_cookies:
                        try:
                            cookies = json.loads(self.current_cookies)
                            await context.add_cookies(cookies)
                            print(f"🍪 Cloud Harvester: Loaded {len(cookies)} cookies.")
                        except json.JSONDecodeError:
                            print("❌ Cloud Harvester: Invalid JSON in cookies.")
                            self.current_cookies = None # Reset invalid cookies
                            await asyncio.sleep(10)
                            continue

                    self.page = await context.new_page()
                    
                    # Setup Request Interception
                    await self.page.route("**/*", self.handle_route)
                    
                    # Navigate to Vertex AI
                    print(f"☁️ Cloud Harvester: Navigating to {VERTEX_URL}...")
                    try:
                        await self.page.goto(VERTEX_URL, timeout=60000, wait_until="domcontentloaded")
                    except Exception as e:
                        print(f"❌ Cloud Harvester: Navigation failed: {e}")
                    
                    # Inner Loop (Session)
                    self.restart_requested = False
                    while self.is_running and not self.restart_requested:
                        # Check for Login Redirection (Cookie Expiry)
                        if "accounts.google.com" in self.page.url or "Sign in" in await self.page.title():
                            print("❌ Cloud Harvester: Cookies Expired or Invalidated by Google (Login Page Detected).")
                            print("   👉 Please export fresh cookies from your browser and update the GOOGLE_COOKIES variable.")
                            # Stop trying to harvest to avoid account lock
                            break

                        # Check if we need to harvest (e.g., every 45 minutes or if credentials are missing)
                        if time.time() - self.last_harvest_time > 2700 or not self.cred_manager.latest_harvest:
                            await self.perform_harvest()
                        
                        await asyncio.sleep(10) # Check every 10 seconds
                    
                    # If we broke out of inner loop, close browser to restart or stop
                    await self.browser.close()
                    if self.restart_requested:
                        print("♻️ Cloud Harvester: Restarting with new cookies...")

            except Exception as e:
                print(f"❌ Cloud Harvester Error: {e}")
                print("♻️ Cloud Harvester: Crashed. Restarting in 10s...")
                await asyncio.sleep(10)
        
        print("☁️ Cloud Harvester: Stopped.")

    async def handle_route(self, route):
        request = route.request
        
        # Check if this is the target request
        if "batchGraphql" in request.url and request.method == "POST":
            try:
                post_data = request.post_data
                if post_data and ("StreamGenerateContent" in post_data or "generateContent" in post_data):
                    print("🎯 Cloud Harvester: Captured Target Request!")
                    
                    # Extract Headers
                    headers = request.headers
                    
                    # Construct Harvest Data
                    harvest_data = {
                        "url": request.url,
                        "method": request.method,
                        "headers": headers,
                        "body": post_data
                    }
                    
                    # Update Credential Manager
                    self.cred_manager.update(harvest_data)
                    self.last_harvest_time = time.time()
                    
            except Exception as e:
                print(f"⚠️ Cloud Harvester: Error analyzing request: {e}")

        await route.continue_()

    async def perform_harvest(self):
        print("🤖 Cloud Harvester: Attempting to trigger request...")
        if not self.page:
            return

        try:
            # --- Popup Handling ---
            # Try to close common popups/dialogs that might block interaction
            print("🧹 Cloud Harvester: Checking for popups...")
            popup_selectors = [
                'button[aria-label="Close"]',
                'button[aria-label="Dismiss"]',
                'button:has-text("Got it")',
                'button:has-text("Not now")',
                'button:has-text("No thanks")',
                'button:has-text("Agree")', # Consent screens
                'div[role="dialog"] button:has-text("Close")',
                'div[role="dialog"] button:has-text("OK")',
                'button:has-text("Accept terms of use")' # Specific Terms button
            ]
            
            # Special handling for "Demo Terms of Use" checkbox
            try:
                # 1. Try to scroll the dialog content to bottom (often required to enable checkbox)
                dialog_content = 'div.mat-mdc-dialog-content'
                if await self.page.is_visible(dialog_content):
                    print("   - Scrolling terms dialog...")
                    await self.page.evaluate(f"document.querySelector('{dialog_content}').scrollTop = document.querySelector('{dialog_content}').scrollHeight")
                    await asyncio.sleep(0.5)

                # 2. Click Checkbox (Aggressive)
                terms_checkbox_label = 'mat-checkbox:has-text("Accept terms of use")'
                if await self.page.is_visible(terms_checkbox_label, timeout=2000):
                    print("   - Found Terms of Use checkbox. Clicking...")
                    # Try standard click first
                    try:
                        await self.page.click(terms_checkbox_label, force=True, timeout=1000)
                    except:
                        # Fallback to JS click
                        print("   - Standard click failed, trying JS click...")
                        await self.page.evaluate(f"document.querySelector('{terms_checkbox_label} input').click()")
                    
                    await asyncio.sleep(1.0)
                    
                    # 3. Click Agree Button (Aggressive)
                    agree_btn = 'button:has-text("Agree")'
                    if await self.page.is_visible(agree_btn):
                        print("   - Clicking Agree button...")
                        # Wait for it to be enabled
                        try:
                            await self.page.wait_for_function(f"document.querySelector('{agree_btn}').disabled === false", timeout=2000)
                        except:
                            print("   - Warning: Agree button might still be disabled.")

                        try:
                            await self.page.click(agree_btn, force=True, timeout=1000)
                        except:
                             await self.page.evaluate(f"document.querySelectorAll('{agree_btn}').forEach(b => b.click())")
                        
                        await asyncio.sleep(2)
            except Exception as e:
                print(f"   - Terms check failed (ignorable): {e}")

            for selector in popup_selectors:
                try:
                    if await self.page.is_visible(selector, timeout=500):
                        print(f"   - Closing popup: {selector}")
                        await self.page.click(selector)
                        await asyncio.sleep(1)
                except:
                    pass
            # ----------------------

            # Wait for editor
            editor_selector = 'div[contenteditable="true"]'
            try:
                await self.page.wait_for_selector(editor_selector, timeout=10000)
            except:
                print("⚠️ Cloud Harvester: Editor not found. Reloading page...")
                await self.page.reload(wait_until="domcontentloaded")
                await asyncio.sleep(5)
                return

            # Type "Hello"
            await self.page.click(editor_selector)
            await self.page.fill(editor_selector, "Hello")
            await asyncio.sleep(1)
            
            # Press Enter
            await self.page.press(editor_selector, "Enter")
            print("🚀 Cloud Harvester: Sent 'Hello' message.")
            
            # Wait a bit to ensure request is captured
            await asyncio.sleep(5)
            
        except Exception as e:
            print(f"❌ Cloud Harvester: Interaction failed: {e}")

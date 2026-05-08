"""
Timely Booking Automation via Playwright
Creates bookings in Timely's admin calendar by automating the booking form.
Runs headlessly on the server after Aria completes a call.
"""

import logging
import asyncio
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

TIMELY_LOGIN_URL = "https://app.gettimely.com/Account/Login"
TIMELY_CALENDAR_URL = "https://app.gettimely.com/calendar"


async def create_timely_booking(
    email: str,
    password: str,
    location_id: str,
    customer_name: str,
    customer_phone: str,
    customer_email: str = "",
    staff_id: str = "486177",  # Mojgan default
    booking_date: str = "",  # e.g. "2026-05-12"
    booking_time: str = "",  # e.g. "10:00am"
    service_name: str = "Injectables Consultation",
) -> dict:
    """Create a booking in Timely via Playwright automation.

    Returns dict with success status and any error message.
    """
    result = {"success": False, "error": None, "details": None}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()

        try:
            # Step 1: Login
            logger.info(f"[timely-book] Logging in...")
            await page.goto(TIMELY_LOGIN_URL, wait_until="networkidle")
            await page.fill("#Email", email)
            await page.fill("#Password", password)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(5000)

            if "calendar" not in page.url.lower():
                result["error"] = f"Login failed, landed on {page.url}"
                return result

            logger.info(f"[timely-book] Logged in, opening new booking form...")

            # Step 2: Open new booking form at the right date
            new_booking_url = (
                f"{TIMELY_CALENDAR_URL}?isNewBooking=True"
                f"&locationId={location_id}&staffId={staff_id}"
            )
            await page.goto(new_booking_url, wait_until="networkidle")
            await page.wait_for_timeout(3000)

            # Step 3: If we need a specific date, navigate to it
            if booking_date:
                # Navigate forward/back to the right date using the calendar nav
                # For now, click on the date in the calendar header
                # The date picker pen icon lets us jump to a date
                try:
                    pen_icon = page.locator('text=Fri, May').first
                    # Just navigate via URL param
                    dated_url = (
                        f"{TIMELY_CALENDAR_URL}?isNewBooking=True"
                        f"&locationId={location_id}&staffId={staff_id}"
                        f"&date={booking_date}"
                    )
                    await page.goto(dated_url, wait_until="networkidle")
                    await page.wait_for_timeout(3000)
                except Exception:
                    pass

            # Step 4: Click on the time slot for the target staff member
            # The calendar shows "Choose a time for the new appointment"
            # Click at the correct time position on the correct staff column
            logger.info(f"[timely-book] Clicking time slot {booking_time}...")

            # Click on the staff column at the target time
            # We'll use coordinate-based clicking based on the time
            # Parse the time to calculate Y position
            clicked = False
            try:
                # Try clicking by matching the time label in the grid
                time_label = booking_time.replace(" ", "")  # "10:00am"
                # The grid has time labels on the left - find the row
                time_cell = page.locator(f'text="{time_label}"').first
                if await time_cell.is_visible():
                    box = await time_cell.bounding_box()
                    if box:
                        # Click to the right of the time label (in the staff column)
                        # Staff column 1 (Mojgan) is roughly x=350
                        await page.mouse.click(350, box["y"] + 10)
                        clicked = True
                        await page.wait_for_timeout(3000)
            except Exception as e:
                logger.warning(f"[timely-book] Time label click failed: {e}")

            if not clicked:
                # Fallback: use coordinate-based approach
                # Parse time to get approximate Y position
                # Calendar starts at ~9am, each 30min is ~30px
                time_str = booking_time.lower().replace(" ", "")
                hour = int(time_str.split(":")[0])
                minute_part = time_str.split(":")[1]
                minutes = int("".join(c for c in minute_part if c.isdigit()))
                if "pm" in time_str and hour != 12:
                    hour += 12

                # Approximate Y: header ~130px, each hour ~60px from 9am
                y_offset = 160 + (hour - 9) * 60 + (minutes / 60) * 60
                await page.mouse.click(350, y_offset)
                await page.wait_for_timeout(3000)

            # Step 5: Check if the booking form opened
            add_appt = page.locator('text="Add appointment"')
            if not await add_appt.is_visible(timeout=5000):
                result["error"] = "Booking form did not open after clicking time slot"
                return result

            logger.info(f"[timely-book] Booking form open, filling details...")

            # Step 6: Fill customer details
            name_field = page.locator('input[placeholder="First and last name"]')
            await name_field.fill(customer_name)
            await page.wait_for_timeout(500)

            mobile_field = page.locator('input[placeholder="Mobile"]')
            await mobile_field.fill(customer_phone)
            await page.wait_for_timeout(500)

            if customer_email:
                email_field = page.locator('input[placeholder="Email"]')
                await email_field.fill(customer_email)
                await page.wait_for_timeout(500)

            # Step 7: Select service (the dropdown is a custom select)
            # The service dropdown has id "Bookings_0__ServiceId"
            try:
                service_select = page.locator("#Bookings_0__ServiceId")
                await service_select.select_option(label=service_name)
                await page.wait_for_timeout(1000)
            except Exception as e:
                logger.warning(f"[timely-book] Service selection failed: {e}")
                # Try partial match
                try:
                    options = await service_select.locator("option").all_text_contents()
                    for opt in options:
                        if service_name.lower() in opt.lower():
                            await service_select.select_option(label=opt)
                            break
                except Exception:
                    pass

            # Step 8: Set status to Confirmed
            try:
                confirmed_btn = page.locator('text="Confirmed"')
                await confirmed_btn.click()
                await page.wait_for_timeout(500)
            except Exception:
                pass

            # Step 9: Intercept the Save POST
            logger.info(f"[timely-book] Clicking Save...")

            async with page.expect_response(
                lambda r: "gettimely.com" in r.url and r.request.method == "POST",
                timeout=15000
            ) as response_info:
                save_btn = page.locator('button:text("Save"), input:text("Save"), .btn-primary:text("Save")')
                await save_btn.click()

            response = await response_info.value
            logger.info(f"[timely-book] Save response: {response.status} {response.url}")

            if response.status in (200, 201, 302):
                result["success"] = True
                result["details"] = {
                    "customer": customer_name,
                    "phone": customer_phone,
                    "time": f"{booking_date} {booking_time}",
                    "service": service_name,
                }
                logger.info(f"[timely-book] Booking created successfully!")
            else:
                result["error"] = f"Save returned status {response.status}"

        except Exception as e:
            logger.exception(f"[timely-book] Error creating booking")
            result["error"] = str(e)

        finally:
            await browser.close()

    return result


def create_booking_sync(**kwargs) -> dict:
    """Synchronous wrapper for the async booking function."""
    return asyncio.run(create_timely_booking(**kwargs))

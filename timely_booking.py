"""
Timely Booking Creation via pure HTTP requests.
No Playwright/Chromium needed. Logs in, grabs anti-forgery token, POSTs form data.
"""

import logging
import requests
import re
import time

logger = logging.getLogger(__name__)

TIMELY_LOGIN_URL = "https://app.gettimely.com/Account/Login"
TIMELY_BOOKING_URL = "https://app.gettimely.com/Calendar/BookingEdit/0"
TIMELY_CALENDAR_URL = "https://app.gettimely.com/calendar"

# Service IDs from the Timely admin form
SERVICE_IDS = {
    "skin_consultation": "3377214:SV",
    "hifu_full_face": "5264337:SV",
    "hifu_face_neck": "5087881:SV",
    "signature_peel": "3359861:SV",
    "skinpen": "3359893:SV",
}

DEFAULT_SERVICE = "skin_consultation"


def timely_login_session(email, password):
    """Login to Timely and return authenticated session."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    })

    # GET login page for CSRF token
    r = session.get(TIMELY_LOGIN_URL, timeout=15)

    token = ""
    match = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r.text)
    if match:
        token = match.group(1)

    # POST login
    login_data = {
        "Email": email,
        "Password": password,
        "RememberMe": "true",
    }
    if token:
        login_data["__RequestVerificationToken"] = token

    r = session.post(TIMELY_LOGIN_URL, data=login_data, allow_redirects=True, timeout=15)

    if "calendar" in r.url.lower() or r.status_code == 200:
        logger.info("[timely-book] Login successful")
        return session

    logger.error(f"[timely-book] Login failed: {r.status_code} -> {r.url}")
    return None


def get_booking_form_tokens(session, location_id, staff_id, date_str, time_str):
    """Load the booking form to get anti-forgery token and hash."""
    # Navigate to calendar with new booking mode
    url = f"{TIMELY_CALENDAR_URL}?isNewBooking=True&locationId={location_id}&staffId={staff_id}"
    r = session.get(url, timeout=15)

    if r.status_code != 200:
        return None, None

    # The booking form is loaded via AJAX when clicking a time slot.
    # Try loading the booking edit page directly
    form_url = (
        f"{TIMELY_BOOKING_URL}"
        f"?staffId={staff_id}"
        f"&date={date_str}"
        f"&time={time_str}"
        f"&locationId={location_id}"
    )
    r = session.get(form_url, timeout=15)

    # Extract __RequestVerificationToken
    token = ""
    match = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r.text)
    if match:
        token = match.group(1)

    # Extract Hash
    hash_val = ""
    match = re.search(r'name="Hash"[^>]*value="([^"]+)"', r.text)
    if match:
        hash_val = match.group(1)

    logger.info(f"[timely-book] Got token: {bool(token)}, hash: {bool(hash_val)}")
    return token, hash_val


def create_timely_booking_requests(
    email,
    password,
    location_id,
    customer_name,
    customer_phone,
    customer_email="",
    staff_id="486177",
    booking_date="",
    booking_time="",
    service_key="skin_consultation",
):
    """Create a booking in Timely using pure HTTP requests."""
    result = {"success": False, "error": None}

    try:
        # Step 1: Login
        session = timely_login_session(email, password)
        if not session:
            result["error"] = "Login failed"
            return result

        # Step 2: Get form tokens
        # Format date for Timely: DD/MM/YYYY
        date_parts = booking_date.split("-")  # YYYY-MM-DD
        if len(date_parts) == 3:
            timely_date = f"{date_parts[2]}/{date_parts[1]}/{date_parts[0]}"
            start_date = f"{date_parts[2]}/{date_parts[1]}/{date_parts[0]} {booking_time}:00"
        else:
            timely_date = booking_date
            start_date = f"{booking_date} {booking_time}:00"

        token, hash_val = get_booking_form_tokens(
            session, location_id, staff_id, timely_date, booking_time
        )

        if not token:
            # Try without the form page - just use the token from login
            logger.warning("[timely-book] No form token, trying with session token")

        # Step 3: Build form data
        service_id = SERVICE_IDS.get(service_key, SERVICE_IDS[DEFAULT_SERVICE])

        # Convert time format: "10:00am" stays as is
        time_formatted = booking_time.lower().replace(" ", "")

        form_data = {
            "__RequestVerificationToken": token,
            "Booking.BookingGroupId": "0",
            "Booking.CustomerId": "0",
            "Booking.Customer.TimeZoneLocaleId": "",
            "Tab": "details",
            "IsInvoiced": "False",
            "UpdateThisOnly": "False",
            "HideUpdateDateButton": "False",
            "CalendarEntryDateTimeModel.StartDate": start_date,
            "ConcessionItemDto": "",
            "IsRebook": "False",
            "ShouldRaiseSale": "False",
            "ShouldSaveAndAddDeposit": "False",
            "Hash": hash_val or "",
            "WaitlistId": "",
            "BusinessCancellationFeeType": "0",
            "CanChargeCancellationFee": "False",
            "ChangesPolicy": "",
            "CancellationFeePerServicePopover": "",
            "CurrencySymbol": "$",
            "UseNewFlow": "False",
            "CancellationFeeAmount": "",
            "Booking.LocationId": location_id,
            "CustomerName": customer_name,
            "Booking.Customer.CustomerTypeId": "1",  # New customer
            "Booking.Customer.Contact.SmsNumber": customer_phone,
            "Booking.Customer.Contact.Telephone": "",
            "Booking.Customer.Contact.Email": customer_email,
            "Booking.Customer.AllowSmsMarketing": "false",
            "Bookings[0].Active": "True",
            "Bookings[0].PaddingStart": "00:00",
            "Bookings[0].PaddingEnd": "00:00",
            "Bookings[0].ServiceId": service_id,
            "Bookings[0].ServiceName": "",
            "Bookings[0].IsServiceGroup": "False",
            "Bookings[0].ServiceGroupId": "0",
            "Bookings[0].IsPartOfServiceGroup": "False",
            "Bookings[0].ServiceGroupItemId": "0",
            "Bookings[0].ProcessingTime": "00:00",
            "Bookings[0].PostTimeDurationTypeId": "1",
            "Bookings[0].BookingLinkEnabled": "False",
            "Bookings[0].CustomerConcessionId": "",
            "Bookings[0].CustomerPackageId": "",
            "Bookings[0].HasOverridePrice": "False",
            "Bookings[0].StaffId": staff_id,
            "Bookings[0].IsStaffRequested": "false",
            "Bookings[0].ResourceId": "-1",
            "Bookings[0].Time": time_formatted,
            "Bookings[0].Length": "00:20",  # 20 min consultation
            "Bookings[0].Price": "",
            "Bookings[1].Active": "False",
            "Bookings[1].PaddingStart": "00:00",
            "Bookings[1].PaddingEnd": "00:00",
            "Bookings[1].ServiceId": "0",
            "Bookings[1].ServiceName": "",
            "Bookings[1].IsServiceGroup": "False",
            "Bookings[1].ServiceGroupId": "0",
            "Bookings[1].IsPartOfServiceGroup": "False",
            "Bookings[1].ServiceGroupItemId": "0",
            "Bookings[1].ProcessingTime": "00:00",
            "Bookings[1].PostTimeDurationTypeId": "1",
            "Bookings[1].BookingLinkEnabled": "False",
            "Bookings[1].CustomerConcessionId": "",
            "Bookings[1].CustomerPackageId": "",
            "Bookings[1].HasOverridePrice": "False",
            "Bookings[1].StaffId": staff_id,
            "Bookings[1].IsStaffRequested": "false",
            "Bookings[1].ResourceId": "-1",
            "Bookings[1].Time": "",
            "Bookings[1].Length": "00:00",
            "Bookings[1].Price": "",
            "Booking.BookingStatusId": "2",  # Confirmed
            "Booking.BookingConfirmationStatusId": "1",  # Not started
            "AutoExpirePencilledInBooking": "false",
            "CalendarRecurrenceModel.CalendarRecurrence.RecurrenceTypeId": "-1",
            "CalendarRecurrenceModel.CalendarRecurrence.Interval": "1",
            "CalendarRecurrenceModel.CalendarRecurrence.RepeatTypeId": "2",
            "CalendarRecurrenceModel.CalendarRecurrence.Occurrences": "1",
            "NoteModel.Note.NoteText": "Booked by Aria AI voice agent",
            "NoteModel.Note.DateCreated": "01/01/0001 00:00:00",
            "LocationContactAddressRequired": "False",
        }

        # Step 4: POST the form
        logger.info(f"[timely-book] POSTing booking: {customer_name} on {booking_date} at {booking_time}")

        r = session.post(
            TIMELY_BOOKING_URL,
            data=form_data,
            allow_redirects=True,
            timeout=30,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"{TIMELY_CALENDAR_URL}?isNewBooking=True&locationId={location_id}",
                "Origin": "https://app.gettimely.com",
            }
        )

        logger.info(f"[timely-book] POST response: {r.status_code}, URL: {r.url}")

        # Check for success - successful booking redirects to calendar
        if r.status_code in (200, 302) and ("calendar" in r.url.lower() or "bookingedit" in r.url.lower()):
            # Check if the response contains error messages
            if "error" in r.text.lower() and "validation" in r.text.lower():
                errors = re.findall(r'<span class="field-validation-error[^"]*">([^<]+)</span>', r.text)
                result["error"] = f"Validation errors: {errors}"
            else:
                result["success"] = True
                result["details"] = {
                    "customer": customer_name,
                    "phone": customer_phone,
                    "time": f"{booking_date} {booking_time}",
                    "service": service_key,
                    "response_url": r.url,
                }
                logger.info(f"[timely-book] Booking created successfully!")
        else:
            result["error"] = f"Unexpected response: {r.status_code} -> {r.url}"

    except Exception as e:
        logger.exception("[timely-book] Error creating booking")
        result["error"] = str(e)

    return result

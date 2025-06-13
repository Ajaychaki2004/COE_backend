from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from datetime import datetime, timedelta
from django.contrib.auth.hashers import make_password, check_password
from pymongo import MongoClient
from dotenv import load_dotenv
import os
import json
import jwt
from bson import ObjectId
import random
import string
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from django.core.mail import send_mail
from django.conf import settings
from django.utils.crypto import get_random_string
from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from datetime import timedelta, timezone as tz
import json
from django.contrib.auth.hashers import make_password
load_dotenv()

JWT_SECRET = 'secret'
JWT_ALGORITHM = 'HS256'

# Database connection
MONGO_URI = os.getenv("MONGO_URI")

# Connect to MongoDB
client = MongoClient(MONGO_URI)
db = client['COE']

subadmin_collection = db["subadmin"]

#======================================================= FUNCTIONS ===========================================================================

def generate_tokens(admin_user, name, college_name):
    """Generates JWT tokens for admin authentication.

    Args:
        admin_user (str): The admin user ID.

    Returns:
        dict: A dictionary containing the JWT token.
    """
    payload = {
        'admin_user': str(admin_user),
        'name': name,
        'role': 'subadmin',
        'college_name': college_name,
        "exp": datetime.utcnow() + timedelta(days=1),
        "iat": datetime.utcnow(),
    }

    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return {'jwt': token}

#======================================================= SUBADMIN ===========================================================================

@csrf_exempt
def subadmin_signin(request):
    """Authenticates an admin user and generates a JWT token.

    Args:
        request (HttpRequest): The HTTP request object.

    Returns:
        JsonResponse: A JSON response containing the JWT token or an error message.
    """
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            email = data.get('email')
            password = data.get('password')

            if not all([email, password]):
                return JsonResponse(
                    {'error': 'Email and password are required'}, status=400)

            # Check for account lockout
            is_locked, remaining_time = check_lockout(email)
            if is_locked:
                return JsonResponse(
                    {'error': f'Account locked. Try again in {remaining_time} minutes.'},
                    status=403)

            subadmin_user = subadmin_collection.find_one({'email': email})

            if not subadmin_user:
                return JsonResponse(
                    {'error': f'Invalid email. No account found with email: {email}'}, status=401)

            if not subadmin_user.get('password') or not subadmin_user.get('email'):
                return JsonResponse(
                    {'error': 'Invalid admin user data'}, status=500)

            if not check_password(password, subadmin_user['password']):
                attempts, lockout_until = increment_login_attempts(email)
                if lockout_until:
                    return JsonResponse(
                        {'error': 'Account locked for 30 minutes due to too many failed attempts'},
                        status=403)
                return JsonResponse(
                    {'error': f'Invalid password. {3 - attempts} attempts remaining'},
                    status=401)

            # Success - reset login attempts and generate token
            reset_login_attempts(email)
            token = generate_tokens(subadmin_user['_id'], subadmin_user['name'], subadmin_user.get('college_name', ''))

            # Update last login time
            subadmin_collection.update_one(
                {'_id': subadmin_user['_id']},
                {'$set': {'last_login': datetime.now()}}
            )

            return JsonResponse({
                'message': 'Logged in successfully',
                'jwt': token['jwt'],
                'last_login': datetime.now()
            }, status=200)

        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'error': 'Invalid request method'}, status=405)

@csrf_exempt
def subadmin_signup(request):
    """Registers a new subadmin_collection user.

    Args:
        request (HttpRequest): The HTTP request object.

    Returns:
        JsonResponse: A JSON response indicating success or failure.
    """
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            print(data)
            name = data.get('name')
            email = data.get('email')
            password = data.get('password')

            if not all([name, email, password]):
                return JsonResponse(
                    {'error': 'All fields are required'}, status=400)

            if subadmin_collection.find_one({'email': email}):
                return JsonResponse(
                    {'error': 'Email already assigned to an admin'}, status=400)

            hashed_password = make_password(password)

            admin_user = {
                'name': name,
                'email': email,
                'password': hashed_password,
                'created_at': datetime.now(),
                'last_login': None
            }

            result = subadmin_collection.insert_one(admin_user)

            return JsonResponse({'message': 'subadmin registered successfully'}, status=201)

        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'error': 'Invalid request method'}, status=405)

def validate_token(token):
    """Validate the token and check if it has expired."""
    subadmin = subadmin_collection.find_one({'password_setup_token': token})

    if not subadmin:
        return False, "Invalid token"

    current_time = timezone.now()  # Aware UTC datetime
    expiry_time = subadmin['password_setup_token_expiry']

    # Ensure expiry_time is aware; if naive, assume UTC
    if expiry_time.tzinfo is None:
        print(f"Warning: Expiry time {expiry_time} is naive, converting to UTC")
        expiry_time = expiry_time.replace(tzinfo=tz.utc)

    # Debug: Log datetimes and timezone info
    print(f"Validate Token - Current time: {current_time}, tzinfo: {current_time.tzinfo}")
    print(f"Validate Token - Expiry time: {expiry_time}, tzinfo: {expiry_time.tzinfo}")

    try:
        if current_time > expiry_time:
            return False, "Token has expired"
    except TypeError as e:
        print(f"TypeError in comparison: {e}")
        return False, "Datetime comparison error"

    return True, "Token is valid"

@csrf_exempt
def validate_password_setup_token(request):
    if request.method == "GET":
        try:
            token = request.GET.get('token')

            if not token:
                return JsonResponse({'error': 'Token is required'}, status=400)

            is_valid, message = validate_token(token)

            if not is_valid:
                return JsonResponse({'error': message}, status=400)

            return JsonResponse({'message': 'Token is valid'}, status=200)

        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'error': 'Invalid request method'}, status=405)

@csrf_exempt
def set_password_for_subadmin(request):
    """Endpoint to set password for subadmin using the token."""
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        data = json.loads(request.body)
        token = data.get('token')
        new_password = data.get('new_password')

        if not all([token, new_password]):
            return JsonResponse({"error": "Missing required fields"}, status=400)

        subadmin = subadmin_collection.find_one({'password_setup_token': token})
        if not subadmin:
            return JsonResponse({"error": "Invalid token"}, status=400)

        current_time = timezone.now()  # Aware UTC datetime
        expiry_time = subadmin['password_setup_token_expiry']

        # Ensure expiry_time is aware; if naive, assume UTC
        if expiry_time.tzinfo is None:
            print(f"Warning: Expiry time {expiry_time} is naive, converting to UTC")
            expiry_time = expiry_time.replace(tzinfo=tz.utc)

        # Debug: Log datetimes
        print(f"Set Password - Current time: {current_time}, tzinfo: {current_time.tzinfo}")
        print(f"Set Password - Expiry time: {expiry_time}, tzinfo: {expiry_time.tzinfo}")

        try:
            if current_time > expiry_time:
                return JsonResponse({"error": "Token has expired"}, status=400)
        except TypeError as e:
            print(f"TypeError in comparison: {e}")
            return JsonResponse({"error": "Datetime comparison error"}, status=500)

        hashed_password = make_password(new_password)
        result = subadmin_collection.update_one(
            {'password_setup_token': token},
            {
                '$set': {'password': hashed_password, 'password_set': True},
                '$unset': {
                    'password_setup_token': "",
                    'password_setup_token_expiry': ""
                }
            }
        )

        if result.modified_count == 0:
            return JsonResponse({"error": "Failed to update password"}, status=500)

        return JsonResponse({"message": "Password set successful"}, status=200)

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


from django.http import HttpRequest

def send_subadmin_setup_email_logic(email: str, full_name: str, token: str) -> tuple[bool, str]:
    """
    Sends a password setup email to a subadmin.
    Returns a tuple of (success: bool, message: str).
    """
    try:
        setup_link = f'http://localhost:5173/subadmin/setup-password?token={token}'
        send_mail(
            subject='Set your password for AI exam analyzer',
            message=f"""
            Hi {full_name},

            Your Subadmin account has been created successfully.

            Please click the following link to set your password: {setup_link}
            This link will expire in 30 minutes.

            Best regards,
            SuperAdmin Team
            """,
            from_email=None,
            recipient_list=[email],
            fail_silently=False,
        )
        return True, "Password setup email sent successfully"
    except Exception as e:
        print(f"Email sending failed with error: {str(e)}")
        return False, f"Failed to send email: {str(e)}"

@csrf_exempt
def send_subadmin_setup_email(request: HttpRequest) -> JsonResponse:
    """
    Sends a password setup email to a subadmin.
    Expects email, full_name, and token in the request body.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        data = json.loads(request.body)
        email = data.get('email')
        full_name = data.get('full_name')
        token = data.get('token')

        if not all([email, full_name, token]):
            return JsonResponse({'error': 'Missing required fields (email, full_name, token)'}, status=400)

        success, message = send_subadmin_setup_email_logic(email, full_name, token)
        if not success:
            return JsonResponse({"error": message}, status=500)

        return JsonResponse({"message": message}, status=200)

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON payload'}, status=400)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@csrf_exempt
def create_subadmin(request: HttpRequest) -> JsonResponse:
    """
    Creates a new subadmin and triggers a password setup email.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        data = json.loads(request.body)
        print("Payload:", data)  # Debug

        full_name = data.get('name')
        email = data.get('email')
        role = data.get('role')
        department = data.get('department')
        designation = data.get('designation')
        college_name = data.get('college_name')

        if not all([full_name, email, role, department, designation]):
            return JsonResponse({'error': 'Missing required fields'}, status=400)

        if subadmin_collection.find_one({'email': email}):
            return JsonResponse({'error': 'A subadmin with this email already exists'}, status=409)

        token = get_random_string(length=32)
        created_time = timezone.now()  # Aware UTC datetime
        expiry_time = created_time + timedelta(minutes=30)  # 30 minutes later

        # Debug: Log creation times
        print(f"Create Subadmin - Created time: {created_time}, tzinfo: {created_time.tzinfo}")
        print(f"Create Subadmin - Expiry time: {expiry_time}, tzinfo: {expiry_time.tzinfo}")

        subadmin = {
            'name': full_name,
            'email': email,
            'role': role,
            'department': department,
            'designation': designation,
            'college_name': college_name,  # Add college_name to document
            'password_set': False,
            'password_setup_token': token,
            'password_setup_token_expiry': expiry_time,
            'created_at': created_time,
            'last_login': None
        }

        result = subadmin_collection.insert_one(subadmin)

        # Send the email using the helper function
        success, message = send_subadmin_setup_email_logic(email, full_name, token)
        if not success:
            # Rollback subadmin creation if email fails
            subadmin_collection.delete_one({'_id': result.inserted_id})
            return JsonResponse({'error': message}, status=500)

        response_data = {
            'id': str(result.inserted_id),
            'name': full_name,
            'email': email,
            'role': role,
            'department': department,
            'designation': designation,
            'college_name': college_name,  # Add college_name to response
            'created_at': created_time.isoformat(),
            'password_setup_token': token,  # Include token for frontend
        }

        print("Subadmin created:", response_data)  # Debug
        return JsonResponse({
            'message': 'Subadmin created successfully. Please check your email to set your password.',
            'subadmin': response_data
        }, status=201)

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON payload'}, status=400)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

# ... (rest of the code remains unchanged)
@csrf_exempt
def get_all_subadmins(request):
    """
    Fetches all subadmin entries from the MongoDB 'subadmin' collection.
    Returns a JSON list of subadmins with their details (excluding passwords).

    Returns a JSON response with the list of subadmins or an error message.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        # Fetch all subadmins from the collection
        subadmins_cursor = subadmin_collection.find({})

        # Convert cursor to list and prepare the response
        subadmins = []
        for subadmin in subadmins_cursor:
            # Convert ObjectId to string for JSON serialization
            subadmin['_id'] = str(subadmin['_id'])

            # Remove sensitive information
            if 'password' in subadmin:
                del subadmin['password']

            subadmins.append(subadmin)

        return JsonResponse({"subadmins": subadmins}, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def get_subadmin_by_id(request, subadmin_id):
    """
    Fetches a specific subadmin by their ID.

    Args:
        request (HttpRequest): The HTTP request object.
        subadmin_id (str): The ID of the subadmin to fetch.

    Returns:
        JsonResponse: A JSON response containing the subadmin data or an error message.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        # Convert string ID to MongoDB ObjectId
        try:
            object_id = ObjectId(subadmin_id)
        except:
            return JsonResponse({"error": "Invalid subadmin ID format"}, status=400)

        # Find the subadmin by ID
        subadmin = subadmin_collection.find_one({"_id": object_id})

        if not subadmin:
            return JsonResponse({"error": "Subadmin not found"}, status=404)

        # Convert ObjectId to string for JSON serialization
        subadmin['_id'] = str(subadmin['_id'])

        # Remove sensitive information
        if 'password' in subadmin:
            del subadmin['password']

        return JsonResponse({"subadmin": subadmin}, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def update_subadmin(request, subadmin_id):
    """
    Updates a specific subadmin's information by their ID.

    Args:
        request (HttpRequest): The HTTP request object.
        subadmin_id (str): The ID of the subadmin to update.

    Returns:
        JsonResponse: A JSON response indicating success or failure.
    """
    if request.method != "PUT" and request.method != "PATCH":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        # Convert string ID to MongoDB ObjectId
        try:
            object_id = ObjectId(subadmin_id)
        except:
            return JsonResponse({"error": "Invalid subadmin ID format"}, status=400)

        # Check if the subadmin exists
        existing_subadmin = subadmin_collection.find_one({"_id": object_id})
        if not existing_subadmin:
            return JsonResponse({"error": "Subadmin not found"}, status=404)

        # Parse the request body
        data = json.loads(request.body)

        # Create update document
        update_data = {}

        # Handle allowed fields to update
        if 'name' in data:
            update_data['name'] = data['name']
        if 'role' in data:
            update_data['role'] = data['role']
        if 'department' in data:
            update_data['department'] = data['department']
        if 'contactNumber' in data:
            update_data['contactNumber'] = data['contactNumber']

        # Handle email update (check for duplicates)
        if 'email' in data and data['email'] != existing_subadmin.get('email'):
            # Check if the new email is already in use
            if subadmin_collection.find_one({"email": data['email']}):
                return JsonResponse({"error": "Email already in use by another subadmin"}, status=409)
            update_data['email'] = data['email']

        # Handle password update
        if 'password' in data and data['password']:
            # Hash the new password
            update_data['password'] = make_password(data['password'])

        # Update the last_modified timestamp
        update_data['last_modified'] = datetime.now()

        # Update the subadmin in the database
        result = subadmin_collection.update_one(
            {"_id": object_id},
            {"$set": update_data}
        )

        if result.modified_count == 0:
            return JsonResponse({"message": "No changes made to the subadmin"}, status=200)

        return JsonResponse({"message": "Subadmin updated successfully"}, status=200)

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def delete_subadmin(request, subadmin_id):
    """
    Deletes a specific subadmin by their ID.

    Args:
        request (HttpRequest): The HTTP request object.
        subadmin_id (str): The ID of the subadmin to delete.

    Returns:
        JsonResponse: A JSON response indicating success or failure.
    """
    if request.method != "DELETE":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        # Convert string ID to MongoDB ObjectId
        try:
            object_id = ObjectId(subadmin_id)
        except:
            return JsonResponse({"error": "Invalid subadmin ID format"}, status=400)

        # Check if the subadmin exists
        existing_subadmin = subadmin_collection.find_one({"_id": object_id})
        if not existing_subadmin:
            return JsonResponse({"error": "Subadmin not found"}, status=404)

        # Delete the subadmin
        result = subadmin_collection.delete_one({"_id": object_id})

        if result.deleted_count == 0:
            return JsonResponse({"error": "Failed to delete subadmin"}, status=500)

        return JsonResponse({"message": "Subadmin deleted successfully"}, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

#======================================================= FORGOT PASSWORD ===========================================================================

def generate_verification_code():
    """Generate a random 6-digit verification code."""
    return ''.join(random.choices(string.digits, k=6))

def store_verification_code(email, code):
    """Store verification code with expiry time (10 minutes)."""
    expiry_time = datetime.now() + timedelta(minutes=10)
    subadmin_collection.update_one(
        {'email': email},
        {
            '$set': {
                'verification_code': code,
                'verification_expiry': expiry_time,
                'verification_attempts': 0
            }
        }
    )

def is_valid_verification_code(email, code):
    """Check if verification code is valid and not expired."""
    admin = subadmin_collection.find_one({'email': email})
    if not admin or 'verification_code' not in admin:
        return False

    if admin['verification_expiry'] < datetime.now():
        return False

    if admin.get('verification_attempts', 0) >= 3:  # Limit attempts
        return False

    # Increment attempts
    subadmin_collection.update_one(
        {'email': email},
        {'$inc': {'verification_attempts': 1}}
    )

    return admin['verification_code'] == code

@csrf_exempt
def subadmin_send_verification_code(request):
    """
    Endpoint to send verification code for password reset.
    Expects email in request body.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        data = json.loads(request.body)
        email = data.get('email')

        if not email:
            return JsonResponse({"error": "Email is required"}, status=400)

        # Check if email exists in admin collection
        admin = subadmin_collection.find_one({'email': email})
        if not admin:
            return JsonResponse({"error": "Email not found"}, status=404)

        # Generate and store verification code
        code = generate_verification_code()
        store_verification_code(email, code)        # Send email with verification code
        try:
            subject = 'Password Reset Verification Code'
            message = f'Your verification code is: {code}\nThis code will expire in 10 minutes.'

            # Create SMTP connection
            server = smtplib.SMTP(settings.EMAIL_HOST, settings.EMAIL_PORT)
            server.ehlo()
            server.starttls()
            server.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)

            # Create email message
            msg = MIMEText(message)
            msg['Subject'] = subject
            msg['From'] = settings.EMAIL_HOST_USER
            msg['To'] = email

            # Send email
            server.send_message(msg)
            server.quit()

            return JsonResponse({"message": "Verification code sent successfully"}, status=200)

        except Exception as e:
            print(f"Email sending failed with error: {str(e)}")  # Log the actual error
            if "SMTPAuthenticationError" in str(e):
                return JsonResponse({"error": "Email authentication failed. Please check email credentials."}, status=500)
            elif "SMTPServerDisconnected" in str(e):
                return JsonResponse({"error": "Failed to connect to email server"}, status=500)
            else:
                return JsonResponse({"error": f"Failed to send email: {str(e)}"}, status=500)

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def subadmin_verify_code_and_reset_password(request):
    """
    Endpoint to verify code and reset password.
    Expects email, verification_code, and new_password in request body.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        data = json.loads(request.body)
        email = data.get('email')
        verification_code = data.get('verification_code')
        new_password = data.get('new_password')

        if not all([email, verification_code, new_password]):
            return JsonResponse({"error": "Missing required fields"}, status=400)

        # Verify the code
        if not is_valid_verification_code(email, verification_code):
            return JsonResponse({"error": "Invalid or expired verification code"}, status=400)

        # Update password and clean up verification fields
        hashed_password = make_password(new_password)
        result = subadmin_collection.update_one(
            {'email': email},
            {
                '$set': {'password': hashed_password},
                '$unset': {
                    'verification_code': "",
                    'verification_expiry': "",
                    'verification_attempts': ""
                }
            }
        )

        if result.modified_count == 0:
            return JsonResponse({"error": "Failed to update password"}, status=500)

        return JsonResponse({"message": "Password reset successful"}, status=200)

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

def reset_login_attempts(email):
    """Reset login attempts for a given email."""
    subadmin_collection.update_one(
        {'email': email},
        {
            '$set': {
                'login_attempts': 0,
                'lockout_until': None
            }
        }
    )

def increment_login_attempts(email):
    """Increment login attempts and set lockout if threshold reached."""
    admin = subadmin_collection.find_one({'email': email})
    current_attempts = admin.get('login_attempts', 0) + 1
    lockout_until = None

    if current_attempts >= 3:  # Lockout threshold
        lockout_until = datetime.now() + timedelta(minutes=30)  # 30-minute lockout

    subadmin_collection.update_one(
        {'email': email},
        {
            '$set': {
                'login_attempts': current_attempts,
                'lockout_until': lockout_until
            }
        }
    )
    return current_attempts, lockout_until

def check_lockout(email):
    """Check if account is locked out."""
    admin = subadmin_collection.find_one({'email': email})
    if not admin:
        return False, None

    lockout_until = admin.get('lockout_until')
    if lockout_until and lockout_until > datetime.now():
        remaining_time = (lockout_until - datetime.now()).seconds // 60
        return True, remaining_time
    return False, None

#======================================================= SEND RESET LINK ===========================================================================

@csrf_exempt
def subadmin_send_reset_link(request):
    """
    Endpoint to send password reset link.
    Expects email in request body.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        data = json.loads(request.body)
        email = data.get('email')

        if not email:
            return JsonResponse({"error": "Email is required"}, status=400)

        # Check if email exists in subadmin collection
        subadmin = subadmin_collection.find_one({'email': email})
        if not subadmin:
            return JsonResponse({"error": "Email not found"}, status=404)

        # Generate a unique token
        token = get_random_string(length=32)

        # Store the token and its expiry time in the database
        expiry_time = datetime.now() + timedelta(hours=1)
        subadmin_collection.update_one(
            {'email': email},
            {'$set': {'reset_token': token, 'reset_token_expiry': expiry_time}}
        )

        # Send email with reset link
        try:
            reset_link = f'http://localhost:5173/subadmin/reset-password?token={token}&email={email}'
            subject = 'Password Reset Link'
            message = f'Click the following link to reset your password: {reset_link}\nThis link will expire in 1 hour.'

            # Create SMTP connection
            server = smtplib.SMTP(settings.EMAIL_HOST, settings.EMAIL_PORT)
            server.ehlo()
            server.starttls()
            server.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)

            # Create email message
            msg = MIMEText(message)
            msg['Subject'] = subject
            msg['From'] = settings.EMAIL_HOST_USER
            msg['To'] = email

            # Send email
            server.send_message(msg)
            server.quit()

            return JsonResponse({"message": "Password reset link sent successfully"}, status=200)

        except Exception as e:
            print(f"Email sending failed with error: {str(e)}")
            return JsonResponse({"error": f"Failed to send email: {str(e)}"}, status=500)

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def subadmin_reset_password(request):
    """
    Endpoint to reset password using the token.
    Expects token, email, and new_password in request body.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        data = json.loads(request.body)
        token = data.get('token')
        email = data.get('email')
        new_password = data.get('new_password')

        if not all([token, email, new_password]):
            return JsonResponse({"error": "Missing required fields"}, status=400)

        # Check if token is valid and not expired
        subadmin = subadmin_collection.find_one({'email': email, 'reset_token': token})

        if not subadmin:
            return JsonResponse({"error": "Invalid token or email not found"}, status=400)

        expiry_time = subadmin.get('reset_token_expiry')
        current_time = datetime.now()

        if expiry_time and expiry_time < current_time:
            return JsonResponse({"error": "Token has expired"}, status=400)

        # Update password and clean up token fields
        hashed_password = make_password(new_password)
        result = subadmin_collection.update_one(
            {'email': email},
            {
                '$set': {'password': hashed_password},
                '$unset': {
                    'reset_token': "",
                    'reset_token_expiry': ""
                }
            }
        )

        if result.modified_count == 0:
            return JsonResponse({"error": "Failed to update password"}, status=500)

        return JsonResponse({"message": "Password reset successful"}, status=200)

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

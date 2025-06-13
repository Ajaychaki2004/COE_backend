from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from datetime import datetime, timedelta
from django.contrib.auth.hashers import make_password, check_password
from pymongo import MongoClient
from dotenv import load_dotenv
import os
import json
import jwt
import random
import string
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from django.core.mail import send_mail
from django.conf import settings
from bson import ObjectId
from django.utils.crypto import get_random_string


load_dotenv()

JWT_SECRET = 'secret'
JWT_ALGORITHM = 'HS256' 

# Database connection
MONGO_URI = os.getenv("MONGO_URI")

# Connect to MongoDB
client = MongoClient(MONGO_URI)
db = client['COE']

superadmin_collection = db["superadmin"]
admin_collection = db["admin"]


#======================================================= FUNCTIONS ===========================================================================

def generate_tokens(superadmin_user, name):
    """Generates JWT tokens for admin authentication.

    Args:
        superadmin_user (str): The admin user ID.

    Returns:
        dict: A dictionary containing the JWT token.
    """
    payload = {
        'superadmin_user': str(superadmin_user),
        'name': name,
        'role': 'superadmin',
        "exp": datetime.utcnow() + timedelta(days=1),
        "iat": datetime.utcnow(),
    }

    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return {'jwt': token}


#======================================================= superADMIN ===========================================================================
def is_valid_verification_code(email, code):
    """Check if verification code is valid and not expired."""
    admin = admin_collection.find_one({'email': email})
    if not admin or 'verification_code' not in admin:
        return False
    
    if admin['verification_expiry'] < datetime.now():
        return False
    
    if admin.get('verification_attempts', 0) >= 3:  # Limit attempts
        return False
        
    # Increment attempts
    admin_collection.update_one(
        {'email': email},
        {'$inc': {'verification_attempts': 1}}
    )
    
    return admin['verification_code'] == code


def increment_login_attempts(email):
    """Increment login attempts and set account to Inactive if threshold reached."""
    admin = superadmin_collection.find_one({'email': email})
    current_attempts = admin.get('login_attempts', 0) + 1
    account_deactivated = False

    print(f"Current attempts for {email}: {current_attempts}")  # Debug
    
    if current_attempts >= 3:  # Deactivation threshold
        
        # Deactivate the account instead of using a time-based lockout
        superadmin_collection.update_one(
            {'email': email},
            {
                '$set': {
                    'login_attempts': current_attempts,
                    'status': 'Inactive'
                }
            }
        )
        account_deactivated = True
    else:
        superadmin_collection.update_one(
            {'email': email},
            {
                '$set': {
                    'login_attempts': current_attempts
                }
            }
        )
    return current_attempts, account_deactivated


def check_account_status(email):
    """Check if account is deactivated."""
    superadmin = superadmin_collection.find_one({'email': email})
    if not superadmin:
        return False
        
    return superadmin.get('status') == 'Inactive'

# @csrf_exempt
# def superadmin_signin(request):
#     """Authenticates an admin user and generates a JWT token.

#     Args:
#         request (HttpRequest): The HTTP request object.

#     Returns:
#         JsonResponse: A JSON response containing the JWT token or an error message.
#     """
#     if request.method == "POST":
#         try:
#             data = json.loads(request.body)
#             email = data.get('email')
#             password = data.get('password')

#             if not all([email, password]):
#                 return JsonResponse(
#                     {'error': 'Email and password are required'}, status=400)


#             superadmin_user = superadmin_collection.find_one({'email': email})

#             if not superadmin_user:
#                 return JsonResponse(
#                     {'error': f'Invalid email. No account found with email: {email}'}, status=401)
            
#             if check_account_status(email):
#                 return JsonResponse(
#                     {'error': 'Account has been deactivated due to too many failed login attempts. Contact the administrator.'},
#                     status=403)

#             if not superadmin_user.get('password') or not superadmin_user.get('email'):
#                 return JsonResponse(
#                     {'error': 'Invalid admin user data'}, status=500)

#             if not check_password(password, superadmin_user['password']):
#                 attempts, account_deactivated = increment_login_attempts(email)
#                 if account_deactivated:
#                     return JsonResponse(
#                         {'error': 'Account has been deactivated due to too many failed attempts. Contact the administrator.'},
#                         status=403)
#                 return JsonResponse(
#                     {'error': f'Invalid password. {3 - attempts} attempts remaining before account deactivation'},
#                     status=401)

#             # Success - generate token
#             reset_login_attempts(email)
#             token = generate_tokens(superadmin_user['_id'], superadmin_user['name'])

#             # Update last login time
#             superadmin_collection.update_one(
#                 {'_id': superadmin_user['_id']},
#                 {'$set': {'last_login': datetime.now()}}
#             )

#             return JsonResponse({
#                 'message': 'Logged in successfully',
#                 'jwt': token['jwt'],
#                 'last_login': datetime.now()
#             }, status=200)

#         except Exception as e:
#             return JsonResponse({'error': str(e)}, status=500)

#     return JsonResponse({'error': 'Invalid request method'}, status=405)


@csrf_exempt
def superadmin_signin(request):
    """Authenticates a superadmin user and generates a JWT token."""
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            email = data.get('email')
            password = data.get('password')
            print(f"Received data - Email: {email}, Password: {password}")  # Debug

            if not email:
                return JsonResponse({'error': 'Email is required'}, status=400)

            if not password:
                return JsonResponse({'error': 'Password is required'}, status=400)

            if check_account_status(email):
                return JsonResponse(
                    {'error': 'Account has been deactivated due to too many failed login attempts. Contact the administrator.'},
                    status=403)

            superadmin_user = superadmin_collection.find_one({'email': email})
            print(f"Superadmin user found: {superadmin_user is not None}")  # Debug

            if not superadmin_user:
                return JsonResponse(
                    {'error': f'Invalid email. No account found with email: {email}'}, status=401)

            if superadmin_user.get('status') != 'Active':
                return JsonResponse(
                    {'error': 'Account is inactive. Contact the administrator.'}, status=403)

            if not superadmin_user.get('password') or not superadmin_user.get('email'):
                return JsonResponse(
                    {'error': 'Invalid superadmin user data'}, status=500)

            if not check_password(password, superadmin_user['password']):
                attempts, account_deactivated = increment_login_attempts(email)
                if account_deactivated:
                    return JsonResponse(
                        {'error': 'Account has been deactivated due to too many failed attempts. Contact the administrator.'},
                        status=403)
                return JsonResponse(
                    {'error': f'Invalid password. {3 - attempts} attempts remaining before account deactivation'},
                    status=401)

            reset_login_attempts(email)
            token = generate_tokens(superadmin_user['_id'], superadmin_user['name'])
            print('Generated token:', token['jwt'])  # Debug

            superadmin_collection.update_one(
                {'_id': superadmin_user['_id']},
                {'$set': {'last_login': datetime.now()}}
            )

            log_superadmin_login(superadmin_user['_id'], email)

            return JsonResponse({
                'message': 'Logged in successfully',
                'jwt': token['jwt'],
                'last_login': datetime.now(),
                'email': email
            }, status=200)

        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON format in request body'}, status=400)

        except Exception as e:
            print(f"Unexpected error: {str(e)}")  # Debug
            return JsonResponse({'error': f'An unexpected error occurred: {str(e)}'}, status=500)

    return JsonResponse({'error': 'Invalid request method'}, status=405)


def log_superadmin_login(superadmin_id, email):
    """Logs the login event for a superadmin."""
    # Implement your logging logic here
    print(f"Superadmin login - ID: {superadmin_id}, Email: {email}")



@csrf_exempt
def superadmin_signup(request):
    """Registers a new superadmin user.

    Args:
        request (HttpRequest): The HTTP request object.

    Returns:
        JsonResponse: A JSON response indicating success or failure.
    """
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            name = data.get('name')
            email = data.get('email')
            password = data.get('password')

            if not all([name, email, password]):
                return JsonResponse(
                    {'error': 'All fields are required'}, status=400)

            if superadmin_collection.find_one({'email': email}):
                return JsonResponse(
                    {'error': 'Email already assigned to an admin'}, status=400)   

            hashed_password = make_password(password)

            admin_user = {
                'name': name,
                'email': email,
                'password': hashed_password,
                'status': "Active",
                'created_at': datetime.now(),
                'last_login': None
            }

            result = superadmin_collection.insert_one(admin_user)

            return JsonResponse({'message': 'Superadmin registered successfully'}, status=201)

        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'error': 'Invalid request method'}, status=405)


#======================================================= FORGOT PASSWORD ===========================================================================

def generate_verification_code():
    """Generate a random 6-digit verification code."""
    return ''.join(random.choices(string.digits, k=6))

def store_verification_code(email, code):
    """Store verification code with expiry time (10 minutes)."""
    expiry_time = datetime.now() + timedelta(minutes=10)
    superadmin_collection.update_one(
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
    admin = superadmin_collection.find_one({'email': email})
    if not admin or 'verification_code' not in admin:
        return False
    
    if admin['verification_expiry'] < datetime.now():
        return False
    
    if admin.get('verification_attempts', 0) >= 3:  # Limit attempts
        return False
        
    # Increment attempts
    superadmin_collection.update_one(
        {'email': email},
        {'$inc': {'verification_attempts': 1}}
    )
    
    return admin['verification_code'] == code

@csrf_exempt
def superadmin_send_verification_code(request):
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
        admin = superadmin_collection.find_one({'email': email})
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
def superadmin_verify_code_and_reset_password(request):
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
        result = superadmin_collection.update_one(
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

#======================================================= A D M I N ===========================================================================

@csrf_exempt
def get_all_admins(request):
    """
    Returns all admin entries from the MongoDB 'admin' collection,
    including the '_id' field as a string.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        admins_cursor = admin_collection.find({})
        admins = []
        for admin in admins_cursor:
            admin['_id'] = str(admin['_id'])
            admins.append(admin)

        return JsonResponse({"admins": admins}, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
    
@csrf_exempt
def get_admin_by_id(request):
    """
    Returns admin details for a given admin ID from the MongoDB 'admin' collection.
    Expects 'id' as a query parameter like ?id=ADMIN_ID
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)
    
    admin_id = request.GET.get('id')
    if not admin_id:
        return JsonResponse({"error": "Admin ID is required"}, status=400)

    try:
        # Convert the ID to ObjectId
        object_id = ObjectId(admin_id)

        # Query the admin document
        admin = admin_collection.find_one({"_id": object_id})
        if not admin:
            return JsonResponse({"error": "Admin not found"}, status=404)

        # Convert ObjectId to string for JSON serialization
        admin['_id'] = str(admin['_id'])
        admin.pop('password', None)

        return JsonResponse({"admin": admin}, status=200)
    
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
    
@csrf_exempt
def toggle_admin_status(request):
    """
    Toggles the admin user's status between 'Active' and 'Inactive' using their _id.
    Sends a custom email notification to the admin based on the new status.
    Expects:
        - Method: POST
        - JSON Body: { "id": "adminObjectId" }
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        body = json.loads(request.body)
        admin_id = body.get("id")

        if not admin_id:
            return JsonResponse({"error": "Admin ID is required"}, status=400)

        admin = admin_collection.find_one({"_id": ObjectId(admin_id)})

        if not admin:
            return JsonResponse({"error": "Admin not found"}, status=404)

        new_status = "Inactive" if admin.get("status") == "Active" else "Active"
        admin_collection.update_one(
            {"_id": ObjectId(admin_id)},
            {"$set": {"status": new_status}}
        )

        # Send email notification
        email = admin.get("email")
        if email:
            try:
                subject = f'Admin Account Status Update - {new_status}'
                if new_status == "Active":
                    message = (
                        f'Dear {admin.get("name", "Admin")},\n\n'
                        f'We are pleased to inform you that your admin account has been activated.\n'
                        f'You can now access the COE Admin Dashboard with full privileges.\n'
                        f'Please log in at http://coe.example.com/admin to manage your tasks.\n\n'
                        f'If you have any questions, please contact the superadmin at support@coe.example.com.\n\n'
                        f'Regards,\nCOE Team'
                    )
                else:  # Inactive
                    message = (
                        f'Dear {admin.get("name", "Admin")},\n\n'
                        f'Your admin account has been deactivated.\n'
                        f'You will no longer have access to the COE Admin Dashboard.\n'
                        f'If you believe this is an error or have questions, please contact the superadmin at support@coe.example.com.\n\n'
                        f'Regards,\nCOE Team'
                    )
                
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
                print(f"Status update email sent to {email}")
            except Exception as e:
                print(f"Failed to send status update email to {email}: {str(e)}")
                # Log the error but don't fail the request
        else:
            print(f"No email found for admin ID {admin_id}")

        return JsonResponse({
            "message": f"Admin status updated to {new_status}",
            "status": new_status
        }, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
        
def reset_login_attempts(email):
    """Reset login attempts for a given email."""
    superadmin_collection.update_one(
        {'email': email},
        {
            '$set': {
                'login_attempts': 0,
                'lockout_until': None
            }
        }
    )

# def increment_login_attempts(email):
#     """Increment login attempts and set lockout if threshold reached."""
#     admin = superadmin_collection.find_one({'email': email})
#     current_attempts = admin.get('login_attempts', 0) + 1
#     lockout_until = None
    
#     if current_attempts >= 3:  # Lockout threshold
#         lockout_until = datetime.now() + timedelta(minutes=30)  # 30-minute lockout
    
#     superadmin_collection.update_one(
#         {'email': email},
#         {
#             '$set': {
#                 'login_attempts': current_attempts,
#                 'lockout_until': lockout_until
#             }
#         }
#     )
#     return current_attempts, lockout_until

def check_lockout(email):
    """Check if account is locked out."""
    admin = superadmin_collection.find_one({'email': email})
    if not admin:
        return False, None
        
    lockout_until = admin.get('lockout_until')
    if lockout_until and lockout_until > datetime.now():
        remaining_time = (lockout_until - datetime.now()).seconds // 60
        return True, remaining_time
    return False, None


#======================================================= SEND RESET LINK ===========================================================================

@csrf_exempt
def superadmin_send_reset_link(request):
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

        # Check if email exists in admin collection
        admin = superadmin_collection.find_one({'email': email})
        if not admin:
            return JsonResponse({"error": "Email not found"}, status=404)

        # Generate a unique token
        token = get_random_string(length=32)

        # Store the token and its expiry time in the database
        expiry_time = datetime.now() + timedelta(hours=1)
        superadmin_collection.update_one(
            {'email': email},
            {'$set': {'reset_token': token, 'reset_token_expiry': expiry_time}}
        )

        # Send email with reset link
        try:
            reset_link = f'http://localhost:5173/superadmin/reset-password?token={token}&email={email}'
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
def superadmin_reset_password(request):
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
        admin = superadmin_collection.find_one({'email': email, 'reset_token': token})
        if not admin or admin.get('reset_token_expiry') < datetime.now():
            return JsonResponse({"error": "Invalid or expired token 1"}, status=400)

        # Update password and clean up token fields
        hashed_password = make_password(new_password)
        result = superadmin_collection.update_one(
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
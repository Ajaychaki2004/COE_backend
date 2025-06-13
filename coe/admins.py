from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from datetime import datetime, timedelta
from django.contrib.auth.hashers import make_password, check_password
from pymongo import MongoClient
from io import TextIOWrapper
from django.views.decorators.http import require_POST
from django.core.mail import send_mail
from django.core.files.storage import default_storage
from bson import ObjectId
from django.core.files.storage import FileSystemStorage
from dotenv import load_dotenv
from django.utils.crypto import get_random_string
import os
import json
import csv
import jwt
from pdf2image import convert_from_bytes
import google.generativeai as genai
import random
import string
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from django.core.mail import send_mail
from django.conf import settings

load_dotenv()

JWT_SECRET = 'secret'
JWT_ALGORITHM = 'HS256'

# Database connection
MONGO_URI = os.getenv("MONGO_URI")

# Connect to MongoDB
client = MongoClient(MONGO_URI)
db = client['COE']

admin_collection = db['admin']
student_collection = db["student"]
subadmin_collection = db["subadmin"]
exam_mapped_questions_collection = db["exam_mapped_questions"]
results_collection = db["results"]
answer_sheet_collection = db["answer_sheets"]
exam_collection = db["exam_details"]

def generate_verification_code():
    """Generate a random 6-digit verification code."""
    return ''.join(random.choices(string.digits, k=6))

def store_verification_code(email, code):
    """Store verification code with expiry time (10 minutes)."""
    expiry_time = datetime.now() + timedelta(minutes=10)
    admin_collection.update_one(
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

def reset_login_attempts(email):
    """Reset login attempts for a given email."""
    admin_collection.update_one(
        {'email': email},
        {
            '$set': {
                'login_attempts': 0
            }
        }
    )

def increment_login_attempts(email):
    """Increment login attempts and set account to Inactive if threshold reached."""
    admin = admin_collection.find_one({'email': email})
    current_attempts = admin.get('login_attempts', 0) + 1
    account_deactivated = False

    if current_attempts >= 3:  # Deactivation threshold
        # Deactivate the account instead of using a time-based lockout
        admin_collection.update_one(
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
        admin_collection.update_one(
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
    admin = admin_collection.find_one({'email': email})
    if not admin:
        return False

    return admin.get('status') == 'Inactive'

#======================================================= FUNCTIONS ===========================================================================

def generate_tokens(admin_user, name, college_name):
    """Generates JWT tokens for admin authentication.

    Args:
        admin_user (str): The admin user ID.
        name (str): The admin user's name.
        college_name (str): The college name associated with the admin.

    Returns:
        dict: A dictionary containing the JWT token.
    """
    payload = {
        'admin_user': str(admin_user),
        'name': name,
        'role': 'admin',
        'college_name': college_name,
        "exp": datetime.utcnow() + timedelta(days=1),
        "iat": datetime.utcnow(),
    }

    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return {'jwt': token}

#======================================================= ADMIN ===========================================================================

import secrets
import string
from datetime import datetime, timedelta
from django.core.mail import send_mail
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.hashers import make_password
import json
from django.utils.crypto import get_random_string
from django.utils import timezone
from datetime import timedelta

import re

def generate_secure_token(length=32):
    """Generate a secure random token."""
    alphabet = string.ascii_letters + string.digits
    token = ''.join(secrets.choice(alphabet) for _ in range(length))

def validate_token(token):
    """Validate the token and check if it has expired."""
    admin_user = admin_collection.find_one({'password_setup_token': token})
    if not admin_user:
        return False, "Invalid token"

    if datetime.now() > admin_user['password_setup_token_expiry']:
        return False, "Token has expired"

    return True, "Token is valid"

def setup_password(token, password):
    """Set the user's password and invalidate the token."""
    is_valid, message = validate_token(token)
    if not is_valid:
        return False, message

    # Validate password complexity
    if len(password) < 8:
        return False, "Password must be at least 8 characters long"
    if not re.search(r'[A-Z]', password):
        return False, "Password must contain at least one uppercase letter"
    if not re.search(r'[a-z]', password):
        return False, "Password must contain at least one lowercase letter"
    if not re.search(r'[0-9]', password):
        return False, "Password must contain at least one number"
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        return False, "Password must contain at least one special character"

    # Hash the password and update the admin user
    hashed_password = make_password(password)
    for user in admin_collection:
        if user['password_token'] == token:
            user.update({
                'password': hashed_password,
                'password_set': True,
                'password_token': None,
                'token_expiry': None,
                'status': "Active"
            })
            break

    return True, "Password set successfully"

# Add these imports at the top of the file

# Update the admin_signup view
@csrf_exempt
def admin_signup(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            name = data.get('name')
            email = data.get('email')
            college_name = data.get('college_name')  # Changed from college_names to college_name

            if not all([name, email, college_name]):
                return JsonResponse({'error': 'Name, email, and college name are required'}, status=400)

            # Check if the email is already assigned to an admin
            if admin_collection.find_one({'email': email}):
                return JsonResponse({'error': 'Email already assigned to an admin'}, status=400)

            # Generate a secure, one-time token for password setup
            token = get_random_string(length=32)
            expiry_time = timezone.now() + timedelta(minutes=30)

            # Create the admin user with pending password setup
            admin_user = {
                'name': name,
                'email': email,
                'college_name': college_name,  # Changed from college_names to college_name
                'password_set': False,
                'password_setup_token': token,
                'password_setup_token_expiry': expiry_time,
                'status': "Active",
                'created_at': datetime.now(),
                'last_login': None
            }

            result = admin_collection.insert_one(admin_user)

            # Send the secure, one-time link to set the password
            setup_link = f'http://localhost:5173/admin/setup-password?token={token}'
            send_mail(
                subject='Set your password for AI exam analyzer',
                message=f"""
                Hi {name},

                Your Admin account has been created successfully.

                Please click the following link to set your password: {setup_link}
                This link will expire in 30 minutes.

                Best regards,
                SuperAdmin Team
                """,
                from_email=None,  # Uses DEFAULT_FROM_EMAIL
                recipient_list=[email],
                fail_silently=False,
            )

            return JsonResponse({'message': 'Admin registered successfully. Please check your email to set your password.'}, status=201)

        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'error': 'Invalid request method'}, status=405)

# Add a new view for validating the password setup token
@csrf_exempt
def validate_password_setup_token(request):
    if request.method == "GET":
        try:
            token = request.GET.get('token')

            if not token:
                return JsonResponse({'error': 'Token is required'}, status=400)

            # Check if the token is valid and not expired
            admin_user = admin_collection.find_one({
                'password_setup_token': token,
                'password_setup_token_expiry': {'$gt': timezone.now()}
            })

            if not admin_user:
                return JsonResponse({'error': 'Invalid or expired token'}, status=400)

            return JsonResponse({'message': 'Token is valid'}, status=200)

        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'error': 'Invalid request method'}, status=405)

# Add a new view for setting the password
@csrf_exempt
def set_password(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            token = data.get('token')
            password = data.get('password')

            if not all([token, password]):
                return JsonResponse({'error': 'Token and password are required'}, status=400)

            # Check if the token is valid and not expired
            admin_user = admin_collection.find_one({
                'password_setup_token': token,
                'password_setup_token_expiry': {'$gt': timezone.now()}
            })

            if not admin_user:
                return JsonResponse({'error': 'Invalid or expired token'}, status=400)

            # Check password complexity (8+ characters, at least one uppercase, one lowercase, one digit)
            if len(password) < 8:
                return JsonResponse({'error': 'Password must be at least 8 characters long'}, status=400)

            if not any(char.isupper() for char in password):
                return JsonResponse({'error': 'Password must contain at least one uppercase letter'}, status=400)

            if not any(char.islower() for char in password):
                return JsonResponse({'error': 'Password must contain at least one lowercase letter'}, status=400)

            if not any(char.isdigit() for char in password):
                return JsonResponse({'error': 'Password must contain at least one digit'}, status=400)

            # Hash the password and update the admin user
            hashed_password = make_password(password)
            admin_collection.update_one(
                {'_id': admin_user['_id']},
                {
                    '$set': {
                        'password': hashed_password,
                        'password_set': True,
                        'password_setup_token': None,
                        'password_setup_token_expiry': None
                    }
                }
            )

            return JsonResponse({'message': 'Password set successfully. You can now log in.'}, status=200)

        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'error': 'Invalid request method'}, status=405)

@csrf_exempt
def password_setup(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            token = data.get('token')
            password = data.get('password')

            success, message = setup_password(token, password)
            if success:
                return JsonResponse({'message': message}, status=200)
            else:
                return JsonResponse({'error': message}, status=400)

        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'error': 'Invalid request method'}, status=405)

# @csrf_exempt
# def admin_signin(request):
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

#             if not email:
#                 return JsonResponse({'error': 'Email is required'}, status=400)

#             if not password:
#                 return JsonResponse({'error': 'Password is required'}, status=400)          # Check if account is deactivated
#             if check_account_status(email):
#                 return JsonResponse(
#                     {'error': 'Account has been deactivated due to too many failed login attempts. Contact the administrator.'},
#                     status=403)

#             admin_user = admin_collection.find_one({'email': email})

#             if not admin_user.get('password_set', False):
#                 return JsonResponse(
#                     {'error': 'Password not set. Please set your password using the link sent to your email.'}, status=403)

#             # Check if admin status is Active
#             if admin_user.get('status') != 'Active':
#                 return JsonResponse(
#                     {'error': 'Account is inactive. Contact the administrator.'}, status=403)

#             if not admin_user.get('password') or not admin_user.get('email'):
#                 return JsonResponse(
#                     {'error': 'Invalid admin user data'}, status=500)

#             if not check_password(password, admin_user['password']):
#                 attempts, account_deactivated = increment_login_attempts(email)
#                 if account_deactivated:
#                     return JsonResponse(
#                         {'error': 'Account has been deactivated due to too many failed attempts. Contact the administrator.'},
#                         status=403)
#                 return JsonResponse(
#                     {'error': f'Invalid password. {3 - attempts} attempts remaining before account deactivation'},
#                     status=401)

#             # Success - reset login attempts and generate token
#             reset_login_attempts(email)
#             token = generate_tokens(admin_user['_id'], admin_user['name'], admin_user['college_name'])
#             print('Generated token:', token['jwt'])  # Debug

#             # Update last login time
#             admin_collection.update_one(
#                 {'_id': admin_user['_id']},
#                 {'$set': {'last_login': datetime.now()}}
#             )

#             return JsonResponse({
#                 'message': 'Logged in successfully',
#                 'jwt': token['jwt'],
#                 'last_login': datetime.now(),
#                 'email': email
#             }, status=200)

#         except json.JSONDecodeError:
#             return JsonResponse({'error': 'Invalid JSON format in request body'}, status=400)

#         except Exception as e:
#             return JsonResponse({'error': f'An unexpected error occurred: {str(e)}'}, status=500)

#     return JsonResponse({'error': 'Invalid request method'}, status=405)

@csrf_exempt
def admin_signin(request):
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

            if not email:
                return JsonResponse({'error': 'Email is required'}, status=400)

            if not password:
                return JsonResponse({'error': 'Password is required'}, status=400)            # Check if account is deactivated
            if check_account_status(email):
                return JsonResponse(
                    {'error': 'Account has been deactivated due to too many failed login attempts. Contact the administrator.'},
                    status=403)
                    
            admin_user = admin_collection.find_one({'email': email})

            if not admin_user:
                return JsonResponse(
                    {'error': f'Invalid email. No account found with email: {email}'}, status=401)

            # Check if admin status is Active
            if admin_user.get('status') != 'Active':
                return JsonResponse(
                    {'error': 'Account is inactive. Contact the administrator.'}, status=403)

            if not admin_user.get('password') or not admin_user.get('email'):
                return JsonResponse(
                    {'error': 'Invalid admin user data'}, status=500)

            if not check_password(password, admin_user['password']):
                attempts, account_deactivated = increment_login_attempts(email)
                if account_deactivated:
                    return JsonResponse(
                        {'error': 'Account has been deactivated due to too many failed attempts. Contact the administrator.'},
                        status=403)
                return JsonResponse(
                    {'error': f'Invalid password. {3 - attempts} attempts remaining before account deactivation'},
                    status=401)

            # Success - reset login attempts and generate token
            reset_login_attempts(email)
            token = generate_tokens(admin_user['_id'], admin_user['name'], admin_user['college_name'])
            print('Generated token:', token['jwt'])  # Debug

            # Update last login time
            admin_collection.update_one(
                {'_id': admin_user['_id']},
                {'$set': {'last_login': datetime.now()}}
            )

            return JsonResponse({
                'message': 'Logged in successfully',
                'jwt': token['jwt'],
                'last_login': datetime.now(),
                'email': email
            }, status=200)

        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON format in request body'}, status=400)

        except Exception as e:
            return JsonResponse({'error': f'An unexpected error occurred: {str(e)}'}, status=500)

    return JsonResponse({'error': 'Invalid request method'}, status=405)

#  ======================================================= Departments ===========================================================================

@csrf_exempt
def create_department(request):
    if request.method != "POST":
        return JsonResponse({'error': 'Invalid method'}, status=405)

    try:
        data = json.loads(request.body)
        department = data.get("department")
        college_name = data.get("college_name")
        hod = data.get("hod") 

        if not all([department, college_name]):
            return JsonResponse({'error': 'Missing required fields'}, status=400)
        
        # Validate department: letters, hyphens, and spaces (but no leading space)
        if not re.fullmatch(r'[A-Za-z][A-Za-z\- ]*', department):
            return JsonResponse({
                'error': 'Department name must start with a letter and contain only letters, hyphens.'
            }, status=400)

        # Validate HOD: letters, dots, and spaces (but no leading space)
        if hod and not re.fullmatch(r'[A-Za-z][A-Za-z. ]*', hod):
            return JsonResponse({
                'error': 'HOD name must be letters'
            }, status=400)

        # Case-insensitive department name check
        if db.department.find_one({
            "department": {"$regex": f"^{department}$", "$options": "i"},
            "college_name": college_name
        }):
            return JsonResponse({'error': 'Department already exists'}, status=409)

        department_doc = {
            "department": department,
            "college_name": college_name,
            "subjects": [],
            "hod": hod or "",
            "created_at": datetime.now(),
            "updated_at": datetime.now()
        }
        result = db.department.insert_one(department_doc)
        return JsonResponse({
            'message': 'Department created successfully',
            '_id': str(result.inserted_id),
            'department': department,
            'college_name': college_name,
            'hod': hod or ""
        }, status=201)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@csrf_exempt
def get_departments_by_college(request):
    if request.method != "POST":
        return JsonResponse({'error': 'Invalid request method'}, status=405)

    try:
        data = json.loads(request.body)
        college_name = data.get("college_name")

        if not college_name:
            return JsonResponse({'error': 'college_name is required in body'}, status=400)

        departments_cursor = db.department.find({"college_name": college_name})
        departments = []

        for dept in departments_cursor:
            departments.append({
                "_id": str(dept["_id"]),
                "department": dept["department"],
                "college_name": dept["college_name"],
                "subjects": dept.get("subjects", []),
                "hod": dept.get("hod", {}),
                "created_at": dept["created_at"].isoformat(),
                "updated_at": dept["updated_at"].isoformat()
            })

        return JsonResponse({"departments": departments}, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@csrf_exempt
def get_department_by_id(request, dept_id):
    if request.method != "GET":
        return JsonResponse({'error': 'Invalid request method'}, status=405)

    try:
        department = db.department.find_one({"_id": ObjectId(dept_id)})

        if not department:
            return JsonResponse({'error': 'Department not found'}, status=404)

        department_info = {
            "_id": str(department["_id"]),
            "department": department["department"],
            "college_name": department["college_name"],
            "subjects": department.get("subjects", []),
            "hod": department.get("hod", {}).get("name", "") if isinstance(department.get("hod"), dict) else department.get("hod", ""),
            "created_at": department["created_at"].isoformat(),
            "updated_at": department["updated_at"].isoformat()
        }

        return JsonResponse({"department": department_info}, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@csrf_exempt
def edit_department(request, dept_id):
    if request.method != "PUT":
        return JsonResponse({'error': 'Invalid method'}, status=405)

    try:
        data = json.loads(request.body)
        new_department = data.get("department")
        new_hod_name = data.get("hod")

        # Validate department: letters, hyphens, and spaces (but no leading space)
        if not re.fullmatch(r'[A-Za-z][A-Za-z\- ]*', new_department):
            return JsonResponse({
                'error': 'Department name must start with a letter and contain only letters, hyphens.'
            }, status=400)

        # Validate HOD: letters, dots, and spaces (but no leading space)
        if new_hod_name and not re.fullmatch(r'[A-Za-z][A-Za-z. ]*', new_hod_name):
            return JsonResponse({
                'error': 'HOD name must be letters'
            }, status=400)

        update_fields = {"updated_at": datetime.now()}

        if new_department:
            update_fields["department"] = new_department
        if new_hod_name :
            update_fields["hod"] = {}
            if new_hod_name:
                update_fields["hod"]["name"] = new_hod_name
            

        result = db.department.update_one(
            {"_id": ObjectId(dept_id)},
            {"$set": update_fields}
        )

        if result.matched_count == 0:
            return JsonResponse({'error': 'Department not found'}, status=404)

        return JsonResponse({'message': 'Department updated successfully'})

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@csrf_exempt
def delete_department(request, dept_id):
    if request.method != "DELETE":
        return JsonResponse({'error': 'Invalid method'}, status=405)

    try:
        department = db.department.find_one({"_id": ObjectId(dept_id)})
        if not department:
            return JsonResponse({'error': 'Department not found'}, status=404)

        if department.get('subjects'):
            return JsonResponse({'error': 'Cannot delete department with subjects'}, status=400)

        if db.student.find_one({"department": department['department']}):
            return JsonResponse({'error': 'Cannot delete department linked to students'}, status=400)

        db.department.delete_one({"_id": ObjectId(dept_id)})
        return JsonResponse({'message': 'Department deleted successfully'})

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

# @csrf_exempt
# def assign_subject_to_department(request):
#     if request.method != "POST":
#         return JsonResponse({'error': 'Invalid method'}, status=405)

#     try:
#         data = json.loads(request.body)
#         dept_id = data.get("dept_id")
#         subject_id = data.get("subject_id")
#         subject_name = data.get("subject_name")
#         semester = data.get("semester")  # Add semester field

#         if not all([dept_id, subject_id, subject_name, semester]):
#             return JsonResponse({'error': 'Missing fields'}, status=400)

#         # Calculate year based on semester
#         if semester < 1 or semester > 8:
#             return JsonResponse({'error': 'Semester must be between 1 and 8'}, status=400)
#         year = 1 if semester in [1, 2] else (semester + 1) // 2

#         department = db.department.find_one({"_id": ObjectId(dept_id)})
#         if not department:
#             return JsonResponse({'error': 'Department not found'}, status=404)

#         # Case-insensitive subject name and ID uniqueness check
#         for subj in department.get("subjects", []):
#             print(f"Checking subject: {subj['subject_name']} (id: {subj['subject_id']}, semester: {subj.get('semester')}) against {subject_name} (id: {subject_id}, semester: {semester})")
#             if subj["subject_name"].lower() == subject_name.lower() and subj.get("semester") == semester:
#                 return JsonResponse({'error': 'Subject name already exists for the given semester in department'}, status=409)
#             if subj["subject_id"] == subject_id and subj.get("semester") == semester:
#                 return JsonResponse({'error': 'Subject ID already exists for the given semester in department'}, status=409)

#         db.department.update_one(
#             {"_id": ObjectId(dept_id)},
#             {
#                 "$push": {
#                     "subjects": {
#                         "subject_id": subject_id,
#                         "subject_name": subject_name,
#                         "semester": semester,  # Add semester to the subject
#                         "year": year  # Add year to the subject
#                     }
#                 },
#                 "$set": {
#                     "updated_at": datetime.now()
#                 }
#             }
#         )

#         return JsonResponse({
#             'message': 'Subject added successfully',
#             'subject': {
#                 'subject_id': subject_id,
#                 'subject_name': subject_name,
#                 'semester': semester,
#                 'year': year
#             }
#         }, status=200)

#     except ValueError as e:
#         print(f"ValueError in assign_subject_to_department: {str(e)}")
#         return JsonResponse({'error': 'Invalid department ID format'}, status=400)
#     except Exception as e:
#         print(f"Error in assign_subject_to_department: {str(e)}")
#         return JsonResponse({'error': str(e)}, status=500)

@csrf_exempt
def search_or_create_subject(request):
    try:
        # Check request method
        if request.method != "POST":
            return JsonResponse({'error': 'Only POST method allowed'}, status=405)

        # Parse JSON body
        if request.content_type and "application/json" in request.content_type:
            try:
                data = json.loads(request.body)
            except json.JSONDecodeError:
                return JsonResponse({'error': 'Invalid JSON format'}, status=400)
        else:
            return JsonResponse({'error': 'Content-Type must be application/json'}, status=400)

        # Required fields for query
        query = {
            "college": data.get("college", ""),
            "year": data.get("year", ""),
            "batch": data.get("batch", ""),
            "department": data.get("department", ""),
            "semester": data.get("semester", ""),
            "section": data.get("section", "")
        }

        # Search for subjects in the department
        department = db["departments"].find_one(query)

        if department and "subjects" in department:
            # If subjects exist, return them
            subjects = [
                {
                    "subject_name": sub.get("subject_name", ""),
                    "subject_code": sub.get("subject_code", "")
                }
                for sub in department["subjects"]
            ]
            return JsonResponse({"subjects": subjects}, status=200)

        # If no subjects exist, create a new subject
        new_subject = {
            "subject_name": data.get("subject_name", ""),
            "subject_code": data.get("subject_code", "")
        }

        # Update the department document with the new subject
        result = db["departments"].update_one(
            query,
            {"$push": {"subjects": new_subject}},
            upsert=True
        )

        if result.upserted_id or result.modified_count > 0:
            return JsonResponse({"message": "Subject created successfully", "subject": new_subject}, status=201)

        return JsonResponse({"error": "Failed to create subject"}, status=500)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@csrf_exempt
def edit_subject(request):
    if request.method != "POST":
        return JsonResponse({'error': 'Invalid method'}, status=405)

    try:
        data = json.loads(request.body)
        dept_id = data.get("dept_id")
        old_subject_id = data.get("old_subject_id")
        new_subject_id = data.get("new_subject_id")
        new_subject_name = data.get("new_subject_name")
        new_semester = data.get("semester")  # Added semester field

        if not all([dept_id, old_subject_id, new_subject_id, new_subject_name, new_semester]):
            return JsonResponse({'error': 'Missing fields'}, status=400)

        if new_semester < 1 or new_semester > 8:
            return JsonResponse({'error': 'Semester must be between 1 and 8'}, status=400)

        # Calculate year based on semester
        new_year = 1 if new_semester in [1, 2] else (new_semester + 1) // 2

        department = db.department.find_one({"_id": ObjectId(dept_id)})
        if not department:
            return JsonResponse({'error': 'Department not found'}, status=404)

        # Check if subject exists
        subject_exists = False
        for subj in department.get("subjects", []):
            if subj["subject_id"] == old_subject_id:
                subject_exists = True
                break
        if not subject_exists:
            return JsonResponse({'error': 'Subject not found'}, status=404)

        # Check for duplicate subject_name or subject_id (excluding the subject being edited)
        for subj in department.get("subjects", []):
            if subj["subject_id"] != old_subject_id:
                print(f"Checking subject: {subj['subject_name']} (id: {subj['subject_id']}) against {new_subject_name} (id: {new_subject_id})")
                if subj["subject_name"].lower() == new_subject_name.lower() and subj.get("semester") == new_semester:
                    return JsonResponse({'error': 'Subject name already exists for the given semester in department'}, status=409)
                if subj["subject_id"] == new_subject_id and subj.get("semester") == new_semester:
                    return JsonResponse({'error': 'Subject ID already exists for the given semester in department'}, status=409)

        # Update subject
        result = db.department.update_one(
            {"_id": ObjectId(dept_id), "subjects.subject_id": old_subject_id},
            {
                "$set": {
                    "subjects.$": {
                        "subject_id": new_subject_id,
                        "subject_name": new_subject_name,
                        "semester": new_semester,
                        "year": new_year
                    },
                    "updated_at": datetime.now()
                }
            }

        )

        if result.matched_count == 0:
            return JsonResponse({'error': 'Subject not found'}, status=404)

        return JsonResponse({
            'message': 'Subject updated successfully',
            'subject': {
                'subject_id': new_subject_id,
                'subject_name': new_subject_name,
                'semester': new_semester,
                'year': new_year
            }
        }, status=200)

    except ValueError as e:
        print(f"ValueError in edit_subject: {str(e)}")
        return JsonResponse({'error': 'Invalid department ID format'}, status=400)
    except Exception as e:
        print(f"Error in edit_subject: {str(e)}")
        return JsonResponse({'error': str(e)}, status=500)

@csrf_exempt
def delete_subject(request):
    if request.method != "POST":
        return JsonResponse({'error': 'Invalid method'}, status=405)

    try:
        data = json.loads(request.body)
        dept_id = data.get("dept_id")
        subject_id = data.get("subject_id")

        if not all([dept_id, subject_id]):
            return JsonResponse({'error': 'Missing fields'}, status=400)

        department = db.department.find_one({"_id": ObjectId(dept_id)})
        if not department:
            return JsonResponse({'error': 'Department not found'}, status=404)

        # Check if subject exists
        subject_exists = False
        for subj in department.get("subjects", []):
            if subj["subject_id"] == subject_id:
                subject_exists = True
                break
        if not subject_exists:
            return JsonResponse({'error': 'Subject not found'}, status=404)

        # Check if subject is linked to students (optional, adjust as needed)
        # Example: if db.student.find_one({"subjects": subject_id}): return JsonResponse({...})

        result = db.department.update_one(
            {"_id": ObjectId(dept_id)},
            {
                "$pull": {
                    "subjects": {
                        "subject_id": subject_id
                    }
                },
                "$set": {
                    "updated_at": datetime.now()
                }
            }
        )

        if result.matched_count == 0:
            return JsonResponse({'error': 'Subject not found'}, status=404)

        return JsonResponse({'message': 'Subject deleted successfully'}, status=200)

    except ValueError as e:
        print(f"ValueError in delete_subject: {str(e)}")
        return JsonResponse({'error': 'Invalid department ID format'}, status=400)
    except Exception as e:
        print(f"Error in delete_subject: {str(e)}")
        return JsonResponse({'error': str(e)}, status=500)

@csrf_exempt
def get_students_by_exam_details(request):
    if request.method == 'POST':
        try:# Get JWT from cookie first
            token = request.COOKIES.get('jwt')

            # If no token in cookies, try Authorization header (Bearer token)
            if not token:
                auth_header = request.headers.get('Authorization')
                if auth_header and auth_header.startswith('Bearer '):
                    token = auth_header.split(' ')[1]

            if not token:
                return JsonResponse({'error': 'JWT token missing'}, status=401)

            # Decode JWT
            try:
                payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
                college = payload.get('college_name')  # ✅ Extract college_name from token
                print("Decoded JWT payload:", college)  #
            except jwt.ExpiredSignatureError:
                return JsonResponse({'error': 'JWT token has expired'}, status=401)
            except jwt.InvalidTokenError:
                return JsonResponse({'error': 'Invalid JWT token'}, status=401)

            # Parse request body
            data = json.loads(request.body)
            department = data.get('department')
            section = data.get('section')
            exam_id = data.get('exam_id')
            subject_code = data.get('subject_code')
            year = data.get('year')

            # Validate input
            if not department:
                return JsonResponse({'error': 'Missing required fields'}, status=400)

            # Build query
            query = {
                'college_name': college,
                'department': department,
                'year': year
            }

            students_cursor = student_collection.find(query)
            students_list = list(students_cursor)
            # print("Found students:", students_list)

            results = []  # ✅ Use a separate list

            for student in students_list:
                student_data = {
                    'id': str(student['_id']),
                    'name': student.get('name', ''),
                    'roll_number': student.get('register_number', ''),
                    'college_name': student.get('college_name', ''),
                }
                print("Student data:", student_data)

                # If exam_id is provided, check if student has submitted an answer sheet
                if exam_id:
                    try:
                        answer_sheet = answer_sheet_collection.find_one({
                            "exam_id": exam_id,
                            "subjects": {
                                "$elemMatch": {
                                    "subject_code": subject_code,
                                    "students.student_id": student.get('register_number')
                                }
                            }
                        })

                        student_data['has_answer_sheet'] = answer_sheet is not None

                        if answer_sheet:
                            # Find the correct subject and student inside the document
                            subject_entry = next(
                                (subj for subj in answer_sheet.get('subjects', [])
                                if subj.get('subject_code') == subject_code),
                                None
                            )
                            student_info = None
                            if subject_entry and isinstance(subject_entry.get('students'), list):
                                student_info = next(
                                    (stu for stu in subject_entry['students']
                                    if stu.get('student_id') == student.get('register_number')),
                                    None
                                )
                            if student_info:
                                student_data['answer_sheet'] = {
                                    "file_url": student_info.get('file_url', ''),
                                    "submitted_at": student_info.get('submitted_at', '').isoformat() if student_info.get('submitted_at') else '',
                                    "subject_code": student_info.get('subject_code', ''),
                                    "status": student_info.get('status', 'submitted'),
                                    "is_evaluated": student_info.get('is_evaluated', False),
                                }
                                print(student_data['has_answer_sheet'])
                    except Exception as e:
                        print(f"Error checking answer sheet for student {student.get('register_number')}: {str(e)}")
                        student_data['has_answer_sheet'] = False

                results.append(student_data)

            return JsonResponse({'students': results}, status=200)

        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)
    else:
        return JsonResponse({'error': 'Method not allowed'}, status=405)

@csrf_exempt
def get_student_by_roll_number(request, roll_number):
    """
    Retrieves a student by their roll/register number.
    Also checks if the student has uploaded answer sheets.

    Args:
        request (HttpRequest): The HTTP request object.
        roll_number (str): The roll number or register number of the student.

    Returns:
        JsonResponse: A JSON response containing the student data or an error message.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        # Find the student by register_number
        student = student_collection.find_one({"register_number": roll_number})

        if not student:
            return JsonResponse({"error": "Student not found"}, status=404)

        # Convert ObjectId to string for JSON serialization
        student['_id'] = str(student['_id'])

        # Check if exam_id is provided as a query parameter
        exam_id = request.GET.get('exam_id')
        subject_code = request.GET.get('subject_code')

        # If exam_id is provided, check if this student has uploaded an answer sheet
        if exam_id:
            try:
                # Find if there's an answer sheet for this student and exam
                answer_sheet = answer_sheet_collection.find_one({
                    "exam_id": ObjectId(exam_id),
                    "student_id": roll_number,
                    "subject_code": subject_code,
                })
                # print(answer_sheet)

                # Add answer sheet status to student data
                student['has_answer_sheet'] = answer_sheet is not None

                # Add answer sheet details if it exists
                if answer_sheet:
                    student['answer_sheet'] = {
                        "id": str(answer_sheet['_id']),
                        "file_name": answer_sheet.get('file_name', ''),
                        "submitted_at": answer_sheet.get('submitted_at', '').isoformat() if answer_sheet.get('submitted_at') else '',
                        "status": answer_sheet.get('status', 'submitted'),
                        "is_evaluated": answer_sheet.get('is_evaluated'),
                    }
            except Exception as e:
                # If there's an error checking answer sheets, just continue without that data
                print(f"Error checking answer sheets: {str(e)}")
                student['has_answer_sheet'] = False

        return JsonResponse({"student": student}, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

#======================================================= FORGOT PASSWORD ===========================================================================

@csrf_exempt
def send_verification_code(request):
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
        admin = admin_collection.find_one({'email': email})
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
def verify_code_and_reset_password(request):
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
        result = admin_collection.update_one(
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

@csrf_exempt
def toggle_subadmin_status(request,subadmin_id):
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
        # admin_id = body.get("id")
        print(f"Received payload: {body}")  # Debugging
        admin_id = subadmin_id

        if not admin_id:
            return JsonResponse({"error": "Admin ID is required"}, status=400)

        admin = subadmin_collection.find_one({"_id": ObjectId(admin_id)})

        if not admin:
            return JsonResponse({"error": "Admin not found"}, status=404)

        new_status = "Inactive" if admin.get("status") == "Active" else "Active"
        subadmin_collection.update_one(
            {"_id": ObjectId(admin_id)},
            {"$set": {"status": new_status}}
        )

        # Send email notification
        # email = admin.get("email")
        # if email:
        #     try:
        #         subject = f'Subadmin Account Status Update - {new_status}'
        #         if new_status == "Active":
        #             message = (
        #                 f'Dear {admin.get("name", "Subadmin")},\n\n'
        #                 f'We are pleased to inform you that your Subadmin account has been activated.\n'
        #                 f'You can now access the COE Subadmin Dashboard with full privileges.\n'
        #                 f'Please log in at http://coe.example.com/Subadmin to manage your tasks.\n\n'
        #                 f'If you have any questions, please contact the superadmin at support@coe.example.com.\n\n'
        #                 f'Regards,\nCOE Team'
        #             )
        #         else:  # Inactive
        #             message = (
        #                 f'Dear {admin.get("name", "Subadmin")},\n\n'
        #                 f'Your Subadmin account has been deactivated.\n'
        #                 f'You will no longer have access to the COE Subadmin Dashboard.\n'
        #                 f'If you believe this is an error or have questions, please contact the superadmin at support@coe.example.com.\n\n'
        #                 f'Regards,\nCOE Team'
        #             )
               
        #         # Create SMTP connection
        #         server = smtplib.SMTP(settings.EMAIL_HOST, settings.EMAIL_PORT)
        #         server.ehlo()
        #         server.starttls()
        #         server.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)
               
        #         # Create email message
        #         msg = MIMEText(message)
        #         msg['Subject'] = subject
        #         msg['From'] = settings.EMAIL_HOST_USER
        #         msg['To'] = email
               
        #         # Send email
        #         server.send_message(msg)
        #         server.quit()
        #         print(f"Status update email sent to {email}")
        #     except Exception as e:
        #         print(f"Failed to send status update email to {email}: {str(e)}")
        #         # Log the error but don't fail the request
        # else:
        #     print(f"No email found for admin ID {admin_id}")

        return JsonResponse({
            "message": f"Admin status updated to {new_status}",
            "status": new_status
        }, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

#======================================================= FORGOT PASSWORD ===========================================================================

@csrf_exempt
def send_verification_code(request):
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
        admin = admin_collection.find_one({'email': email})
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
def verify_code_and_reset_password(request):
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
        result = admin_collection.update_one(
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

#======================================================= SEND RESET LINK ===========================================================================

@csrf_exempt
def send_reset_link(request):
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
        admin = admin_collection.find_one({'email': email})
        if not admin:
            return JsonResponse({"error": "Email not found"}, status=404)

        # Generate a unique token
       
        token = get_random_string(length=32)

        # Store the token and its expiry time in the database
        expiry_time = datetime.now() + timedelta(hours=1)
        admin_collection.update_one(
            {'email': email},
            {'$set': {'reset_token': token, 'reset_token_expiry': expiry_time}}
        )

        # Send email with reset link
        try:
            reset_link = f'http://localhost:5173/admin/reset-password?token={token}&email={email}'
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
def reset_password(request):
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
        admin = admin_collection.find_one({'email': email, 'reset_token': token})
        if not admin or admin.get('reset_token_expiry') < datetime.now():
            return JsonResponse({"error": "Invalid or expired token 1"}, status=400)

        # Update password and clean up token fields
        hashed_password = make_password(new_password)
        result = admin_collection.update_one(
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


#======================================================== ASSIGN SUBJECT TO DEPARTMENT ===========================================================================


@csrf_exempt
def assign_subject_to_department(request):
    """
    Assigns/appends a subject to a department's subjects array.
    Validates input data and checks for duplicates based on subject_id, subject_name, and semester.
    """
    if request.method != "POST":
        return JsonResponse({'error': 'Invalid method'}, status=405)

    try:
        data = json.loads(request.body)
        dept_id = data.get("dept_id")
        subject_id = data.get("subject_id")
        subject_name = data.get("subject_name")
        semester = data.get("semester")

        if not all([dept_id, subject_id, subject_name, semester]):
            return JsonResponse({'error': 'Missing required fields: dept_id, subject_id, subject_name, semester'}, status=400)

        # Validate semester range
        if not isinstance(semester, int) or semester < 1 or semester > 8:
            return JsonResponse({'error': 'Semester must be an integer between 1 and 8'}, status=400)

        # Calculate year based on semester
        year = 1 if semester in [1, 2] else (semester + 1) // 2

        # Validate subject_id and subject_name format
        if not re.match(r'^[A-Za-z0-9]+$', subject_id):
            return JsonResponse({'error': 'Subject ID must contain only alphanumeric characters'}, status=400)
        
        if not re.match(r'^[A-Za-z0-9\s\-&]+$', subject_name):
            return JsonResponse({'error': 'Subject name contains invalid characters'}, status=400)

        # Find the department
        try:
            department = db.department.find_one({"_id": ObjectId(dept_id)})
        except Exception as e:
            return JsonResponse({'error': 'Invalid department ID format'}, status=400)

        if not department:
            return JsonResponse({'error': 'Department not found'}, status=404)

        # Check for duplicate subject_id or subject_name within the same semester
        for existing_subject in department.get("subjects", []):
            if existing_subject["subject_id"].lower() == subject_id.lower():
                return JsonResponse({
                    'error': f'Subject ID "{subject_id}" already exists in this department'
                }, status=409)
            
            if (existing_subject["subject_name"].lower() == subject_name.lower() and 
                existing_subject.get("semester") == semester):
                return JsonResponse({
                    'error': f'Subject "{subject_name}" already exists for semester {semester} in this department'
                }, status=409)

        # Create new subject object
        new_subject = {
            "subject_id": subject_id,
            "subject_name": subject_name,
            "semester": semester,
            "year": year
        }

        # Append the subject to the department's subjects array
        result = db.department.update_one(
            {"_id": ObjectId(dept_id)},
            {
                "$push": {"subjects": new_subject},
                "$set": {"updated_at": datetime.now()}
            }
        )

        if result.matched_count == 0:
            return JsonResponse({'error': 'Department not found'}, status=404)

        if result.modified_count == 0:
            return JsonResponse({'error': 'Failed to add subject to department'}, status=500)

        return JsonResponse({
            'message': 'Subject added successfully to department',
            'subject': new_subject,
            'department_id': dept_id
        }, status=200)

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON format'}, status=400)
    except Exception as e:
        print(f"Error in assign_subject_to_department: {str(e)}")
        return JsonResponse({'error': str(e)}, status=500)
    



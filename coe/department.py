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
import pandas as pd
import re

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

@csrf_exempt
def bulk_upload_subjects(request):
    """
    Bulk upload subjects to a department from CSV/Excel file.
    Expected file format: Subject Code, Subject Name, Semester
    """
    if request.method != "POST":
        return JsonResponse({'error': 'Invalid method'}, status=405)

    try:
        dept_id = request.POST.get("dept_id")
        if not dept_id:
            return JsonResponse({'error': 'Missing department ID'}, status=400)

        # Validate department exists
        try:
            department = db.department.find_one({"_id": ObjectId(dept_id)})
        except Exception as e:
            return JsonResponse({'error': 'Invalid department ID format'}, status=400)

        if not department:
            return JsonResponse({'error': 'Department not found'}, status=404)

        # Get uploaded file
        if 'file' not in request.FILES:
            return JsonResponse({'error': 'No file uploaded'}, status=400)

        uploaded_file = request.FILES['file']
        
        # Validate file type
        allowed_extensions = ['.csv', '.xlsx', '.xls']
        file_extension = os.path.splitext(uploaded_file.name)[1].lower()
        
        if file_extension not in allowed_extensions:
            return JsonResponse({'error': 'Invalid file format. Please upload CSV or Excel file'}, status=400)

        # Parse file based on extension
        try:
            if file_extension == '.csv':
                df = pd.read_csv(uploaded_file)
            else:
                df = pd.read_excel(uploaded_file)
        except Exception as e:
            return JsonResponse({'error': f'Error reading file: {str(e)}'}, status=400)

        # Validate required columns
        required_columns = ['Subject Code', 'Subject Name', 'Semester']
        if not all(col in df.columns for col in required_columns):
            return JsonResponse({
                'error': f'Missing required columns. Expected: {", ".join(required_columns)}'
            }, status=400)

        # Process each row
        subjects_to_add = []
        errors = []
        existing_subjects = department.get("subjects", [])
        
        for index, row in df.iterrows():
            row_num = index + 2  # Excel/CSV row number (starting from 2 due to header)
            
            try:
                subject_id = str(row['Subject Code']).strip()
                subject_name = str(row['Subject Name']).strip()
                semester = int(row['Semester'])
                
                # Validate required fields
                if not subject_id or subject_id == 'nan':
                    errors.append(f"Row {row_num}: Subject Code is required")
                    continue
                    
                if not subject_name or subject_name == 'nan':
                    errors.append(f"Row {row_num}: Subject Name is required")
                    continue
                
                # Validate semester range
                if semester < 1 or semester > 8:
                    errors.append(f"Row {row_num}: Semester must be between 1 and 8")
                    continue
                
                # Validate subject_id format
                if not re.match(r'^[A-Za-z0-9]+$', subject_id):
                    errors.append(f"Row {row_num}: Subject ID must contain only alphanumeric characters")
                    continue
                
                # Validate subject_name format
                if not re.match(r'^[A-Za-z0-9\s\-&]+$', subject_name):
                    errors.append(f"Row {row_num}: Subject name contains invalid characters")
                    continue
                
                # Check for duplicates in existing subjects
                duplicate_found = False
                for existing_subject in existing_subjects:
                    if existing_subject["subject_id"].lower() == subject_id.lower():
                        errors.append(f"Row {row_num}: Subject ID '{subject_id}' already exists")
                        duplicate_found = True
                        break
                    
                    if (existing_subject["subject_name"].lower() == subject_name.lower() and 
                        existing_subject.get("semester") == semester):
                        errors.append(f"Row {row_num}: Subject '{subject_name}' already exists for semester {semester}")
                        duplicate_found = True
                        break
                
                if duplicate_found:
                    continue
                
                # Check for duplicates in current batch
                batch_duplicate = False
                for batch_subject in subjects_to_add:
                    if batch_subject["subject_id"].lower() == subject_id.lower():
                        errors.append(f"Row {row_num}: Duplicate Subject ID '{subject_id}' in file")
                        batch_duplicate = True
                        break
                    
                    if (batch_subject["subject_name"].lower() == subject_name.lower() and 
                        batch_subject.get("semester") == semester):
                        errors.append(f"Row {row_num}: Duplicate Subject '{subject_name}' for semester {semester} in file")
                        batch_duplicate = True
                        break
                
                if batch_duplicate:
                    continue
                
                # Calculate year based on semester
                year = 1 if semester in [1, 2] else (semester + 1) // 2
                
                # Add to subjects list
                subjects_to_add.append({
                    "subject_id": subject_id,
                    "subject_name": subject_name,
                    "semester": semester,
                    "year": year
                })
                
            except ValueError as e:
                errors.append(f"Row {row_num}: Invalid data format - {str(e)}")
            except Exception as e:
                errors.append(f"Row {row_num}: Error processing row - {str(e)}")

        # If there are validation errors, return them
        if errors:
            return JsonResponse({
                'error': 'Validation errors found',
                'errors': errors,
                'processed_count': 0,
                'uploaded_count': 0
            }, status=400)

        # If no subjects to add
        if not subjects_to_add:
            return JsonResponse({
                'message': 'No valid subjects found to upload',
                'processed_count': len(df),
                'uploaded_count': 0
            }, status=200)

        # Bulk insert subjects to department
        try:
            result = db.department.update_one(
                {"_id": ObjectId(dept_id)},
                {
                    "$push": {"subjects": {"$each": subjects_to_add}},
                    "$set": {"updated_at": datetime.now()}
                }
            )
            
            if result.matched_count == 0:
                return JsonResponse({'error': 'Department not found'}, status=404)
            
            if result.modified_count == 0:
                return JsonResponse({'error': 'Failed to add subjects to department'}, status=500)

            return JsonResponse({
                'message': f'Successfully uploaded {len(subjects_to_add)} subjects',
                'processed_count': len(df),
                'uploaded_count': len(subjects_to_add),
                'subjects': subjects_to_add
            }, status=200)

        except Exception as e:
            return JsonResponse({
                'error': f'Database error: {str(e)}',
                'processed_count': len(df),
                'uploaded_count': 0
            }, status=500)

    except Exception as e:
        print(f"Error in bulk_upload_subjects: {str(e)}")
        return JsonResponse({'error': str(e)}, status=500)

@csrf_exempt
def get_department_student(request, dept_id=None):
    """
    Fetch all students belonging to a specific college and department.
    Filters students based on college_name, department, and batch (all mandatory).
    """
    if request.method != "POST":
        return JsonResponse({'error': 'Invalid method'}, status=405)

    try:
        # Parse request body
        data = json.loads(request.body)
        college_name = data.get("college_name")
        department = data.get("department")
        batch = data.get("batch")

        # Make all fields mandatory
        if not college_name or not department or not batch:
            return JsonResponse({'error': 'Missing required fields: college_name, department, batch'}, status=400)

        # Normalize batch format to handle spaces around dash
        # Convert "2022-2026" to match "2022 - 2026" format
        normalized_batch = batch.replace("-", " - ").replace("  ", " ").strip()
        
        # Create flexible batch regex pattern that matches both formats
        batch_pattern = batch.replace("-", r"\s*-\s*").replace(" ", r"\s*")

        # Query students based on college_name, department, and batch
        query = {
            "college_name": {"$regex": f"^{re.escape(college_name)}$", "$options": "i"},
            "department": {"$regex": f"^{re.escape(department)}$", "$options": "i"},
            "batch": {"$regex": f"^{batch_pattern}$", "$options": "i"}
        }

        students_cursor = student_collection.find(query)

        # Convert cursor to list and format the data
        students_list = []
        for student in students_cursor:
            # Calculate semester and year based on year field
            year_str = str(student.get("year", "I")).strip().upper()
            
            # Handle different year formats and calculate semester
            year_mapping = {
                "I": {"year_num": 1, "semester": 1},
                "1": {"year_num": 1, "semester": 1},
                "FIRST": {"year_num": 1, "semester": 1},
                "II": {"year_num": 2, "semester": 3},
                "2": {"year_num": 2, "semester": 3},
                "SECOND": {"year_num": 2, "semester": 3},
                "III": {"year_num": 3, "semester": 5},
                "3": {"year_num": 3, "semester": 5},
                "THIRD": {"year_num": 3, "semester": 5},
                "IV": {"year_num": 4, "semester": 7},
                "4": {"year_num": 4, "semester": 7},
                "FOURTH": {"year_num": 4, "semester": 7}
            }
            
            year_info = year_mapping.get(year_str, {"year_num": 1, "semester": 1})
            
            # Generate batch if not present (fallback logic)
            student_batch = student.get("batch", "")
            if not student_batch:
                # Generate batch based on year and admission year
                current_year = datetime.now().year
                admission_year = current_year - year_info["year_num"] + 1
                student_batch = f"{admission_year}-{admission_year + 4}"
            
            student_data = {
                "student_id": student.get("register_number", ""),
                "student_name": student.get("name", ""),
                "email": student.get("email", ""),
                "semester": year_info["semester"],
                "year": year_info["year_num"],
                "batch": student_batch,  # Added batch field
                "section": student.get("section", ""),
                "college_name": student.get("college_name", ""),
                "department": student.get("department", ""),
                "register_number": student.get("register_number", ""),
                "status": student.get("status", "Active"),
                "updated_at": student.get("updated_at")
            }
            students_list.append(student_data)

        # Sort students by batch, year, section, and then by student_name
        students_list.sort(key=lambda x: (x["batch"], x["year"], x.get("section", ""), x["student_name"]))

        return JsonResponse({
            'success': True,
            'message': f'Found {len(students_list)} students',
            'students': students_list,
            'count': len(students_list),
            'filters': {
                'college_name': college_name,
                'department': department,
                'batch': batch
            }
        }, status=200)

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON format'}, status=400)
    except Exception as e:
        print(f"Error in get_department_student: {str(e)}")
        return JsonResponse({'error': f'Internal server error: {str(e)}'}, status=500)

@csrf_exempt
def get_unique_batches(request):
    """
    Fetch unique batch values from the exam_collection filtered by department only.
    Returns a list of all unique batch values found for the specified department.
    """
    if request.method != "POST":
        return JsonResponse({'error': 'Invalid method'}, status=405)

    try:
        # Parse request body
        data = json.loads(request.body)
        department = data.get("department")

        if not department:
            return JsonResponse({'error': 'Missing required field: department'}, status=400)

        # Create filter query for exam collection (only department filtering)
        filter_query = {
            "department": {"$regex": f"^{re.escape(department)}$", "$options": "i"}
        }

        # Get distinct/unique batch values from exam_collection with filters
        unique_batches = exam_collection.distinct("batch", filter_query)
        
        # Filter out None/null values and empty strings
        filtered_batches = [batch for batch in unique_batches if batch and str(batch).strip()]
        
        # Sort the batches for better presentation
        filtered_batches.sort()

        return JsonResponse({
            'success': True,
            'message': f'Found {len(filtered_batches)} unique batches for {department} department',
            'batches': filtered_batches,
            'count': len(filtered_batches),
            'filters': {
                'department': department
            }
        }, status=200)

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON format'}, status=400)
    except Exception as e:
        print(f"Error in get_unique_batches: {str(e)}")
        return JsonResponse({'error': f'Internal server error: {str(e)}'}, status=500)






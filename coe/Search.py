from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from django.conf import settings
from datetime import datetime, timedelta
from django.contrib.auth.hashers import make_password, check_password
from pymongo import MongoClient
from io import TextIOWrapper
from django.views.decorators.http import require_POST
from django.core.files.storage import default_storage
from bson import ObjectId
from django.core.files.storage import FileSystemStorage
from dotenv import load_dotenv
import os
import json
import re
import pdfplumber
import jwt
import tempfile
from pdf2image import convert_from_bytes
import google.generativeai as genai
import base64
import zipfile
import logging
import concurrent.futures
import threading
from queue import Queue
import uuid
import io
import boto3
import PIL.Image
import numpy as np
import cv2
from pdf2image import convert_from_path, pdfinfo_from_path
from io import BytesIO
import time  # Add this import for time.sleep()
import random  # Add this for potential jitter in retry logic
import os
import json
import smtplib
from io import BytesIO
from datetime import datetime
from bson import ObjectId
from email.mime.text import MIMEText
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
import tempfile
import jwt



load_dotenv()

POPPLER_PATH = r"C:\Program Files\poppler-24.08.0\Library\bin"

def process_pdf_request(request):
    pdf_file = request.POST.get('pdf_path')
    info = pdfinfo_from_path(pdf_file, poppler_path=POPPLER_PATH)
    images = convert_from_path(pdf_file, poppler_path=POPPLER_PATH)
    return info, images

JWT_SECRET = 'secret'
JWT_ALGORITHM = 'HS256'

# Database connection
MONGO_URI = os.getenv("MONGO_URI")

# Global variables to track bulk processing jobs
bulk_upload_jobs = {}
evaluation_jobs = {}
MAX_WORKERS = 5  # Maximum parallel workers

# Connect to MongoDB
client = MongoClient(MONGO_URI)
db = client['COE']

student_collection = db["student"]
exam_collection = db["exam_details"]
department_collection = db["department"]

def search_specified_collections(search_term):
    """
    Search for the given term in specified collections and fields.
    Returns matching documents excluding date fields.
    """
    results = {}
    search_term_lower = search_term.lower()
    
    # Search in student collection with active status filter
    student_query = {
        "$and": [
            # Only return students who are active or don't have a status field
            {"$or": [
                {"status": "Active"},
                {"status": {"$exists": False}}
            ]},
            {"$or": [
                {"name": {"$regex": search_term, "$options": "i"}},
                {"email": {"$regex": search_term, "$options": "i"}},
                {"register_number": {"$regex": search_term, "$options": "i"}},
                {"department": {"$regex": search_term, "$options": "i"}}  # Added department as search keyword
            ]}
        ]
    }
    student_results = list(student_collection.find(student_query))
    if student_results:
        results["student"] = student_results
    
    # Search in exam collection
    exam_query = {
        "$or": [
            {"exam_type": {"$regex": search_term, "$options": "i"}},
            {"subjects.subject_name": {"$regex": search_term, "$options": "i"}},
            {"subjects.subject_code": {"$regex": search_term, "$options": "i"}}
        ]
    }
    exam_results = list(exam_collection.find(exam_query))
    if exam_results:
        results["exam"] = exam_results
    
    # Search in department collection
    department_query = {
        "$or": [
            {"department": {"$regex": search_term, "$options": "i"}},
            {"college_name": {"$regex": search_term, "$options": "i"}},
            {"subjects.subject_name": {"$regex": search_term, "$options": "i"}},
            {"subjects.subject_id": {"$regex": search_term, "$options": "i"}}  # Added this line to search subject_id
        ]
    }
    department_results = list(department_collection.find(department_query))
    if department_results:
        results["department"] = department_results
    
    # Optimize subject search by using MongoDB query instead of Python filtering
    # This avoids fetching all departments and exams when we only need matching subjects
    
    # Search for subjects in departments with direct query
    department_subject_query = {
        "subjects": {
            "$elemMatch": {
                "$and": [
                    # Only return subjects with non-empty fields
                    {"subject_id": {"$ne": ""}},
                    {"subject_name": {"$ne": ""}},
                    # Match the search term in either field
                    {"$or": [
                        {"subject_name": {"$regex": search_term, "$options": "i"}},
                        {"subject_id": {"$regex": search_term, "$options": "i"}}
                    ]}
                ]
            }
        }
    }
    
    subject_results = []
    department_projection = {"_id": 0, "department": 1, "college_name": 1, "subjects": 1}
    
    # Get matching subjects from departments using optimized query
    for dept in department_collection.find(department_subject_query, department_projection):
        if "subjects" in dept:
            # Extract only the matching subjects
            for subject in dept.get("subjects", []):
                if isinstance(subject, dict) and subject.get("subject_id") and subject.get("subject_name"):
                    if (search_term_lower in subject.get("subject_name", "").lower() or
                        search_term_lower in subject.get("subject_id", "").lower()):
                        subject_results.append({
                            "subject_id": subject.get("subject_id", ""),
                            "subject_name": subject.get("subject_name", ""),
                            "department": dept.get("department", ""),
                            "college_name": dept.get("college_name", ""),
                            "source": "department"
                        })
    
    # Only include department subjects by filtering out exam subjects
    subject_results = [subj for subj in subject_results if subj.get("source") == "department"]
    
    if subject_results:
        results["subject"] = subject_results
    
    return results

def search_specified_collections_for_admin(search_term, college_name):
    """
    Search for the given term in specified collections and fields,
    filtered by the admin's college name.
    Returns matching documents excluding date fields.
    """
    results = {}
    search_term_lower = search_term.lower()
    
    # Search in student collection with college filter and active status
    student_query = {
        "$and": [
            {"college_name": college_name},
            # Only return students who are active or don't have a status field
            {"$or": [
                {"status": "Active"},
                {"status": {"$exists": False}}
            ]},
            {"$or": [
                {"name": {"$regex": search_term, "$options": "i"}},
                {"email": {"$regex": search_term, "$options": "i"}},
                {"register_number": {"$regex": search_term, "$options": "i"}},
                {"department": {"$regex": search_term, "$options": "i"}}  # Added department as search keyword
            ]}
        ]
    }
    student_results = list(student_collection.find(student_query))
    if student_results:
        results["student"] = student_results
    
    # Search in exam collection with college filter
    exam_query = {
        "$and": [
            {"college": college_name},
            {"$or": [
                {"exam_type": {"$regex": search_term, "$options": "i"}},
                {"subjects.subject_name": {"$regex": search_term, "$options": "i"}},
                {"subjects.subject_code": {"$regex": search_term, "$options": "i"}}
            ]}
        ]
    }
    exam_results = list(exam_collection.find(exam_query))
    if exam_results:
        results["exam"] = exam_results
    
    # Search in department collection with college filter
    department_query = {
        "$and": [
            {"college_name": college_name},
            {"$or": [
                {"department": {"$regex": search_term, "$options": "i"}},
                {"college_name": {"$regex": search_term, "$options": "i"}},
                {"subjects.subject_name": {"$regex": search_term, "$options": "i"}},
                {"subjects.subject_id": {"$regex": search_term, "$options": "i"}}
            ]}
        ]
    }
    department_results = list(department_collection.find(department_query))
    if department_results:
        results["department"] = department_results
    
    # Optimize subject search for admin with direct query and college filter
    # Search for subjects in departments with direct query and college filter
    department_subject_query = {
        "college_name": college_name,
        "subjects": {
            "$elemMatch": {
                "$and": [
                    # Only return subjects with non-empty fields
                    {"subject_id": {"$ne": ""}},
                    {"subject_name": {"$ne": ""}},
                    # Match the search term in either field
                    {"$or": [
                        {"subject_name": {"$regex": search_term, "$options": "i"}},
                        {"subject_id": {"$regex": search_term, "$options": "i"}}
                    ]}
                ]
            }
        }
    }
    
    subject_results = []
    department_projection = {"_id": 0, "department": 1, "college_name": 1, "subjects": 1}
    
    # Get matching subjects from departments using optimized query
    for dept in department_collection.find(department_subject_query, department_projection):
        if "subjects" in dept:
            # Extract only the matching subjects
            for subject in dept.get("subjects", []):
                if isinstance(subject, dict) and subject.get("subject_id") and subject.get("subject_name"):
                    if (search_term_lower in subject.get("subject_name", "").lower() or
                        search_term_lower in subject.get("subject_id", "").lower()):
                        subject_results.append({
                            "subject_id": subject.get("subject_id", ""),
                            "subject_name": subject.get("subject_name", ""),
                            "department": dept.get("department", ""),
                            "college_name": dept.get("college_name", ""),
                            "source": "department"
                        })
    
    # Only include department subjects by filtering out exam subjects
    subject_results = [subj for subj in subject_results if subj.get("source") == "department"]
    
    if subject_results:
        results["subject"] = subject_results
    
    return results

@csrf_exempt
@require_POST
def search_data_in_all_collections(request):
    """
    Django view to search for a term in specific collections.
    Expects JSON body with 'search_term'.
    """
    try:
        data = json.loads(request.body)
        search_term = data.get('search_term', '')
        if not search_term:
            return JsonResponse({'error': 'search_term is required'}, status=400)
        
        results = search_specified_collections(search_term)
        
        # Convert ObjectId to string and exclude date fields for JSON serialization
        def convert_for_json(doc):
            processed_doc = {}
            for k, v in doc.items():
                # Skip date fields
                if isinstance(v, dict) and "$date" in v:
                    continue
                elif isinstance(v, ObjectId):
                    processed_doc[k] = str(v)
                elif isinstance(v, bytes):
                    processed_doc[k] = base64.b64encode(v).decode('utf-8')
                elif isinstance(v, list):
                    # Process lists (e.g., subjects in department collection)
                    processed_doc[k] = [convert_for_json(item) if isinstance(item, dict) else item for item in v]
                elif isinstance(v, dict):
                    # Process nested dictionaries
                    processed_doc[k] = convert_for_json(v)
                else:
                    processed_doc[k] = v
            return processed_doc
        
        for collection_name in results:
            if collection_name == 'student':
                # Format student collection results in specified structure
                formatted_results = []
                for doc in results[collection_name]:
                    formatted_doc = {
                        "name": doc.get("name", ""),
                        "department": doc.get("department", ""),
                        "college_name": doc.get("college_name", ""),
                        "year": doc.get("year", ""),
                        "section": doc.get("section", ""),
                        "email": doc.get("email", ""),
                        "register_number": doc.get("register_number", ""),
                        "status": "Active"  # Adding status as Active for all students
                    }
                    formatted_results.append(formatted_doc)
                results[collection_name] = formatted_results
            elif collection_name == 'department':
                # Format department collection results in specified structure
                formatted_results = []
                for doc in results[collection_name]:
                    # Ensure subjects array is properly converted
                    subjects = []
                    for subject in doc.get("subjects", []):
                        if isinstance(subject, dict):
                            subjects.append({
                                "subject_id": subject.get("subject_id", ""),
                                "subject_name": subject.get("subject_name", "")
                            })
                    
                    formatted_doc = {
                        "_id": str(doc.get("_id", "")),  # Include the Object ID
                        "department": doc.get("department", ""),
                        "college_name": doc.get("college_name", ""),
                        "subjects": subjects
                    }
                    formatted_results.append(formatted_doc)
                results[collection_name] = formatted_results
            elif collection_name == 'exam':
                # Format exam collection results in specified structure
                formatted_results = []
                for doc in results[collection_name]:
                    # Ensure subjects and answer_sheets are properly converted
                    subjects = []
                    for subject in doc.get("subjects", []):
                        if isinstance(subject, dict):
                            subjects.append({
                                "subject_name": subject.get("subject_name", ""),
                                "subject_code": subject.get("subject_code", "")
                            })
                    
                    answer_sheets = []
                    for sheet in doc.get("answer_sheets", []):
                        if isinstance(sheet, dict):
                            answer_sheets.append({
                                "student_id": sheet.get("student_id", ""),
                                "student_name": sheet.get("student_name", "")
                            })
                    
                    formatted_doc = {
                        "_id": str(doc.get("_id", "")),  # Include the Object ID
                        "exam_type": doc.get("exam_type", ""),
                        "college": doc.get("college", ""),
                        "department": doc.get("department", ""),
                        "year": doc.get("year", ""),
                        "semester": doc.get("semester", ""),
                        "section": doc.get("section", ""),
                        "subjects": subjects,
                        "answer_sheets": answer_sheets
                    }
                    formatted_results.append(formatted_doc)
                results[collection_name] = formatted_results
            elif collection_name == 'subject':
                # Subject results are already formatted appropriately
                pass
            else:
                # Use existing conversion for other collections
                results[collection_name] = [convert_for_json(doc) for doc in results[collection_name]]
            
        return JsonResponse({'results': results}, status=200)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@csrf_exempt
@require_POST
def search_data_for_admin(request):
    """
    Django view for admin search, filtering data by their college.
    Expects JSON body with 'search_term' and authorization header with JWT token.
    """
    try:
        # Get authorization header
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return JsonResponse({'error': 'Authentication required'}, status=401)
        
        # Extract and verify token
        token = auth_header.split(' ')[1]
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            college_name = payload.get('college_name')
            
            if not college_name:
                return JsonResponse({'error': 'College name not found in token'}, status=400)
                
        except jwt.PyJWTError as e:
            return JsonResponse({'error': f'Authentication failed: {str(e)}'}, status=401)
        
        # Get search term from request body
        data = json.loads(request.body)
        search_term = data.get('search_term', '')
        if not search_term:
            return JsonResponse({'error': 'search_term is required'}, status=400)
        
        # Search collections with college filter
        results = search_specified_collections_for_admin(search_term, college_name)
        
        # Convert ObjectId to string and exclude date fields for JSON serialization
        def convert_for_json(doc):
            processed_doc = {}
            for k, v in doc.items():
                # Skip date fields
                if isinstance(v, dict) and "$date" in v:
                    continue
                elif isinstance(v, ObjectId):
                    processed_doc[k] = str(v)
                elif isinstance(v, bytes):
                    processed_doc[k] = base64.b64encode(v).decode('utf-8')
                elif isinstance(v, list):
                    # Process lists (e.g., subjects in department collection)
                    processed_doc[k] = [convert_for_json(item) if isinstance(item, dict) else item for item in v]
                elif isinstance(v, dict):
                    # Process nested dictionaries
                    processed_doc[k] = convert_for_json(v)
                else:
                    processed_doc[k] = v
            return processed_doc
        
        for collection_name in results:
            if collection_name == 'student':
                # Format student collection results in specified structure
                formatted_results = []
                for doc in results[collection_name]:
                    formatted_doc = {
                        "name": doc.get("name", ""),
                        "department": doc.get("department", ""),
                        "college_name": doc.get("college_name", ""),
                        "year": doc.get("year", ""),
                        "section": doc.get("section", ""),
                        "email": doc.get("email", ""),
                        "register_number": doc.get("register_number", ""),
                        "status": "Active"  # Adding status as Active for all students
                    }
                    formatted_results.append(formatted_doc)
                results[collection_name] = formatted_results
            elif collection_name == 'department':
                # Format department collection results in specified structure
                formatted_results = []
                for doc in results[collection_name]:
                    # Ensure subjects array is properly converted
                    subjects = []
                    for subject in doc.get("subjects", []):
                        if isinstance(subject, dict):
                            subjects.append({
                                "subject_id": subject.get("subject_id", ""),
                                "subject_name": subject.get("subject_name", "")
                            })
                    
                    formatted_doc = {
                        "_id": str(doc.get("_id", "")),
                        "department": doc.get("department", ""),
                        "college_name": doc.get("college_name", ""),
                        "subjects": subjects
                    }
                    formatted_results.append(formatted_doc)
                results[collection_name] = formatted_results
            elif collection_name == 'exam':
                # Format exam collection results in specified structure
                formatted_results = []
                for doc in results[collection_name]:
                    # Ensure subjects and answer_sheets are properly converted
                    subjects = []
                    for subject in doc.get("subjects", []):
                        if isinstance(subject, dict):
                            subjects.append({
                                "subject_name": subject.get("subject_name", ""),
                                "subject_code": subject.get("subject_code", "")
                            })
                    
                    answer_sheets = []
                    for sheet in doc.get("answer_sheets", []):
                        if isinstance(sheet, dict):
                            answer_sheets.append({
                                "student_id": sheet.get("student_id", ""),
                                "student_name": sheet.get("student_name", "")
                            })
                    
                    formatted_doc = {
                        "_id": str(doc.get("_id", "")),
                        "exam_type": doc.get("exam_type", ""),
                        "college": doc.get("college", ""),
                        "department": doc.get("department", ""),
                        "year": doc.get("year", ""),
                        "semester": doc.get("semester", ""),
                        "section": doc.get("section", ""),
                        "subjects": subjects,
                        "answer_sheets": answer_sheets
                    }
                    formatted_results.append(formatted_doc)
                results[collection_name] = formatted_results
            elif collection_name == 'subject':
                # Remove exam_collection data or standardize the format
                formatted_results = []
                for doc in results[collection_name]:
                    # Keep only the necessary fields and standardize the structure
                    formatted_doc = {
                        "subject_id": doc.get("subject_id", ""),
                        "subject_name": doc.get("subject_name", ""),
                        "department": doc.get("department", ""),
                        "college_name": doc.get("college_name", "")
                        # "source" field is removed to standardize the result
                    }
                    formatted_results.append(formatted_doc)
                results[collection_name] = formatted_results
            else:
                # Use existing conversion for other collections
                results[collection_name] = [convert_for_json(doc) for doc in results[collection_name]]
            
        return JsonResponse({'results': results, 'college_filter': college_name}, status=200)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
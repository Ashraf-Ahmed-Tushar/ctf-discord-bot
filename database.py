from pymongo import MongoClient
import os

MONGO_URI = os.getenv("MONGO_URI")

client = MongoClient(MONGO_URI)

db = client["ctfbot"]

verified = db["verified_users"]
pending = db["pending_verify"]

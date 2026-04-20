import boto3

session = boto3.Session()
print("region:", session.region_name)
print("creds present:", session.get_credentials() is not None)

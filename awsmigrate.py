#!/usr/bin/env python3

import configparser
import os
import uuid

import boto3
import inquirer
from botocore.exceptions import ClientError

conf = configparser.ConfigParser()
configfile = os.path.expanduser('~/.aws/config')
conf.read(configfile)
acctchoices = conf.sections()

os.system('clear')

# Choose source account
accounts = [
    inquirer.List('srcacct',
                  message="Choose the source account",
                  choices=acctchoices,
                  ),
]
accountselections = inquirer.prompt(accounts)
srcprofile = accountselections["srcacct"].replace("profile ", "")

# Start srcaccount session
srcsession = boto3.Session(profile_name=srcprofile)
srcr53client = srcsession.client('route53')

# Get list of Hosted Zones from source account
try:
    hostedzones = srcr53client.list_hosted_zones()

    srczonenamelist = []
    srczonenamedict = {}
    for zone in hostedzones['HostedZones']:
        zname = zone['Name']
        zid = zone['Id'].replace("/hostedzone/", "")
        znameclean = zname[:-1]
        srczonenamelist.append(znameclean)
        srczonenamedict[znameclean] = zid
        srczonenamedict[zid] = znameclean
except ClientError as e:
    print(e.response['Error']['Code'])
    exit()

os.system('clear')
# Choose zone to move
whichzone = [
    inquirer.List(
        'zonetomove', message="Move which zone", choices=srczonenamelist,
    ),
]

zoneselection = inquirer.prompt(whichzone)
zonetomove = zoneselection["zonetomove"]
srczoneid = srczonenamedict[zonetomove]

# Select Destination Account
os.system('clear')
dstaccounts = [
    inquirer.List(
        'dstacct', message="Choose the destination account", choices=acctchoices,
    ),
]
dstaccountselection = inquirer.prompt(dstaccounts)
dstprofile = dstaccountselection["dstacct"].replace("profile ", "")

# Start dest account session
dstsession = boto3.Session(profile_name=dstprofile)
dstr53client = dstsession.client('route53')

# See if zone exist in destination?
dstzoneid = None
try:
    doeszoneexists = dstr53client.list_hosted_zones_by_name()
    for zone in doeszoneexists['HostedZones']:
        if zonetomove in zone['Name']:
            # Zone Exists, grab the Destination Zone ID
            dstzoneid = zone['Id'].replace("/hostedzone/", "")
except ClientError as e:
    print(e.response['Error']['Code'])

# Start Migration
os.system('clear')
print("Start migration of " + zonetomove + " from " + srcprofile + " to " + dstprofile)
print()
# Hosted Zone does not exist on destination and it must be created.
if dstzoneid is None:
    # Create zone on destination side.
    callref = uuid.uuid4()
    createzoneresponse = None
    try:
        createzoneresponse = dstr53client.create_hosted_zone(
            Name=zonetomove,
            CallerReference=str(callref),
            HostedZoneConfig={
                'Comment': 'Migrated from srcprofile',
                'PrivateZone': False
            }
        )
    except ClientError as e:
        print(e.response['Error']['Code'])
        exit()
    # Zone Created
    print("Created Zone")
    print("\tName: " + createzoneresponse['HostedZone']['Name'])
    dstzoneid = createzoneresponse['HostedZone']['Id']
    print("\tId: " + dstzoneid)
    print()

# Fetch records from source
print("Migrating zone records for " + zonetomove)
sourcezonepaginator = srcr53client.get_paginator('list_resource_record_sets')
try:
    dstzonechanges = list()
    source_zone_records = sourcezonepaginator.paginate(HostedZoneId=srczoneid)
    for record_set in source_zone_records:
        for record in record_set['ResourceRecordSets']:
            if record['Type'] != 'NS' and record['Type'] != 'SOA':
                dstzonechange = {}
                dstzonechange['Action'] = 'CREATE'
                dstzonechange['ResourceRecordSet'] = {}
                dstzonechange['ResourceRecordSet']['Name'] = record['Name']
                dstzonechange['ResourceRecordSet']['Type'] = record['Type']
                dstzonechange['ResourceRecordSet']['TTL'] = 300
                resreclist = list()
                for rr in record['ResourceRecords']:
                    resrecdict = {}
                    resrecdict['Value'] = rr['Value']
                    resreclist.append(resrecdict)
                dstzonechange['ResourceRecordSet']['ResourceRecords'] = resreclist
                dstzonechanges.append(dstzonechange)

except ClientError as e:
    print(e.response['Error']['Code'])

# Build changebatch
changebatch = {}
changebatch['Comment'] = 'Migrated using aws dns migrator'
changebatch['Changes'] = dstzonechanges

# Add to Destination
try:
    adddestrecords = dstr53client.change_resource_record_sets(
        HostedZoneId=dstzoneid,
        ChangeBatch=changebatch
    )
except ClientError as e:
    print(e.response['Error']['Code'])
    exit()

print("Hosted Zone and Records Transferred to " + dstprofile)
print()

# Should have transfer auth code, lets check and
transmessage = "Transfer " + zonetomove + " to " + dstprofile + "?"
transquestion = [
    inquirer.Confirm('continue', message=transmessage)
]
transanswers = inquirer.prompt(transquestion)

# Get Dest Info
try:
    destzonepaginator = dstr53client.get_paginator('list_resource_record_sets')
    dest_zone_records = destzonepaginator.paginate(HostedZoneId=dstzoneid)
    transfernameservers = list()
    displaynslist = list()
    for record_set in dest_zone_records:
        for record in record_set['ResourceRecordSets']:
            if record['Type'] == 'NS':
                for ns in record['ResourceRecords']:
                    nameserverdict = {}
                    nameserverdict['Name'] = ns['Value']
                    displaynslist.append(ns['Value'])
                    nameserverdict['GlueIps'] = list()
                    transfernameservers.append(nameserverdict)
except ClientError as e:
    print("Problem Fetching Destination Domain Details")
    print(e.response['Error']['Code'])
    exit()

if not transanswers:
    print("You'll want to either manually transfer the domain, or change the nameservers at the registrar to:")
    for dns in displaynslist:
        print("\t" + dns)
    exit()

# See if we can start the domain transfer process as well
transferauthcode = None
try:
    srcr53domainclient = srcsession.client('route53domains')
    fetchdomainauthcode = srcr53domainclient.retrieve_domain_auth_code(
        DomainName=zonetomove
    )
    transferauthcode = fetchdomainauthcode['AuthCode']
except ClientError as e:
    Print("Problem fetching auth code.")
    print(e.response['Error']['Code'])
    exit()

# transfer
if transferauthcode is not None:
    dstr53domainclient = dstsession.client('route53domains')
    # Get Contact Info
    try:
        domaindetails = srcr53domainclient.get_domain_detail(
            DomainName=zonetomove
        )
        print("Moving Contacts Over")
    except ClientError as e:
        print("Problem Fetching Src Domain Details")
        print(e.response['Error']['Code'])
        exit()

    # Transfer Domain
    print("Sending Transfer Request.")
    try:
        transferdomain = dstr53domainclient.transfer_domain(
            DomainName=zonetomove,
            DurationInYears=1,
            Nameservers=transfernameservers,
            AuthCode=transferauthcode,
            AutoRenew=True,
            AdminContact=domaindetails['AdminContact'],
            RegistrantContact=domaindetails['RegistrantContact'],
            TechContact=domaindetails['TechContact'],
            PrivacyProtectAdminContact=True,
            PrivacyProtectRegistrantContact=True,
            PrivacyProtectTechContact=True
        )
    except ClientError as e:
        print("Problem Transferring Domain")
        print(e.response['Error']['Code'])
        exit()

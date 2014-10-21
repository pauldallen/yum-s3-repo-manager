#!/usr/bin/python

# Copyright 2014: Lithium Technologies, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
#
# Author(s):
#   - Matthew Bogner (matthew.bogner@lithium.com)
#   - Paul Allen (paul.allen@lithium.com)
#
# Description:
#
#   Manages a yum repo based in Amazon S3.  This script eliminates a lot of the race conditions that come
#   up when you have multiple sources updating the repository's metadata files.  It solves that problem by
#   being the single entity to perform the writing and updating of the repository itself by watching an
#   "inbox" that others can place RPMs into.
#
#   All that it takes to publish a new RPM into the repository is to place the new RPM into the inbox. Any
#   sub-directories used in the inbox will be mimiced when the RPM is moved over to the repo itself.

# Deps requiring separate install
import boto                                  # https://github.com/boto/boto#installation
import boto.utils
from boto.exception import S3ResponseError
from flask import Flask, url_for, jsonify    # http://flask.pocoo.org/
from apscheduler.scheduler import Scheduler  # http://pythonhosted.org/APScheduler/#installing-apscheduler
from dogapi import dog_http_api as api       # pip install dogapi

# Deps part of core python
import logging
import ConfigParser
import __main__
import os
import sys
import subprocess
import datetime
import time
import json
from optparse import OptionParser
from collections import deque

app = Flask(__name__)

logging.basicConfig()

version = "1.0.2"

def parseArgs():
    parser = OptionParser("usage: %prog [options]")
    parser.add_option("-b", "--bucket-name",        dest="yumRepoBucketNames",  default=[], action="append",                     help="The list of names of an existing S3 bucket. *NOTE* all buckets must be in the same region [default: %default]")
    parser.add_option("--bucket-region",            dest="yumRepoBucketRegion", default="us-west-1",                             help="The region of the S3 bucket [default: %default]")
    parser.add_option("-i", "--inbox-folder-name",  dest="inboxFolderName",     default="inbox",                                 help="The relative folder name for the inbox inside the S3 bucket.  No preceding or trailing slash necessary. (i.e. \"inbox\") [default: %default]")
    parser.add_option("-r", "--repo-folder-name",   dest="repoFolderName",      default="repo",                                  help="The relative folder name for the actual repo inside the S3 bucket.  No preceding or trailing slash necessary. (i.e. \"repo\") [default: %default]")
    parser.add_option("-l", "--local-staging-area", dest="localStagingArea",    default="/tmp/repostaging",                      help="The fully qualified path of an existing local folder where the repository work will be performed.  No trailing slash necessary. (i.e. \"/tmp/repostaging\") [default: %default]")
    parser.add_option("-c", "--cachedir",           dest="localCache",          default="/media/ephemeral0/repocache",           help="The fully qualified path of an existing local folder where the repository cache can be stored (preferrably SSD or ephemeral disk).  No trailing slash necessary. (i.e. \"/tmp/repostaging\") [default: %default]")
    parser.add_option("-t", "--time-interval",      dest="timeInterval",        default="60",  type="int",                       help="The time interval and seconds between polling [default: %default]")
    parser.add_option("-d", "--debug",              dest="debug",               default=False, action="store_true",              help="Whether or not to run the app in debug mode [default: %default]")
    parser.add_option("-v", "--version",            dest="version",             default=False, action="store_true",              help="Output the version of this script and exit")
    parser.add_option("--log-file",                 dest="logFile",             default="/var/log/yum-repo-manager/manager.log", help="The log file in which to dump debug information [default: %default]")
    parser.add_option("--pruneAgeDays",             dest="pruneAgeDays",        default="60",  type="int",                       help="Prune packages from the repo that are older than this number of days and for which we have more than --keep-at-least copies of it [default: %default]")
    parser.add_option("--keep-at-least",            dest="keepAtLeast",         default="10",  type="int",                       help="Keep at least this many copies of each individual package [default: %default]")
    parser.add_option("--datadog-api-key",          dest="datadogApiKey",       default=None,                                    help="Datadog API key to publish metrics [default: %default]")
    parser.add_option("--datadog-app-key",          dest="datadogAppKey",       default=None,                                    help="Datadog application key to publish metrics [default: %default]")
    parser.add_option("--per-folder",               dest="perFolder",           default=False, action="store_true",              help="Setup per folder repo. [default: %default]")
    return parser.parse_args()

"""
This method is intended to record in datadog the count of any type of keys found in s3.
The only two types used right now are the count of keys proccessed in the inbox, and the count
of keys skipped due to any sort of exception
"""
def recordKeys(keys, keyType = 'inbox', yumRepoBucketName):
    details = {}
    keyTypeLabel = "%sKeys" % keyType
    details["timestamp"] = str(datetime.datetime.now())
    details[keyTypeLabel] = len(keys)
    keyNames = []
    if len(keys) > 0:
        for key in keys:
            keyNames.append(key.name)
    details[keyTypeLabel] = keyNames

    lastRuns.append(details)
    if len(lastRuns) > 60:
        lastRuns.popleft()

    if options.datadogApiKey != None:
        api.api_key = options.datadogApiKey
        api.application_key = options.datadogAppKey
        api.timeout = 15
        api.swallow = False
        metricName = "cloudops.yumrepomanager.%s.%s" % (yumRepoBucketName.replace('-', '_'), keyTypeLabel)
        log("Publishing metric to datadog: %s..." % metricName)
        log("   --> %s" % json.dumps(api.metric(metricName, len(keys))))

def chompLeft(original, removeFromLeft):
    if original.startswith(removeFromLeft):
        return original[len(removeFromLeft):]
    return original

def log(statement):
    if not os.path.exists(os.path.dirname(options.logFile)):
        os.makedirs(os.path.dirname(options.logFile))
    logFile = open(options.logFile, 'a')
    ts = datetime.datetime.now()
    isFirst = True
    for line in statement.split("\n"):
        if isFirst:
            logFile.write("%s - %s\n" % (ts, line))
            isFirst = False
        else:
            logFile.write("%s -    %s\n" % (ts, line))
    logFile.close()

"""
Get a connection to S3 through one of two possible methods.

    Method 1: If we are in AWS - just create a connection relying on IAM instance profiles to provide
              the necessary levels of authentication and authorization
    Method 2: If not in AWS - read the regular AWS CLI's config file to get the access key and secret key.
"""
def getS3Cxn():
    log("Obtaining S3 connection...")
    # If we are in AWS, then rely on instance profiles to provide the creds for us. Otherwise, read them from the standard AWS CLI configs.
    #if len(boto.utils.get_instance_metadata(timeout=1, num_retries=0).keys()) > 0:
    try:
        execute(["/opt/aws/bin/ec2-metadata", "-z"])
        return boto.connect_s3()
    except OSError, e:
        # Parse the main AWS CLI config file
        config = ConfigParser.ConfigParser()
        config.read(os.path.expanduser('~/.aws/config'))
        profileName = "lithiumdev"
        sectionName = "profile %s" % profileName

        # Read the authentication args from the main AWS CLI config file so that boto doesn't need its own
        accessKeyId = config.get(sectionName, "aws_access_key_id")
        secretAccessKey = config.get(sectionName, "aws_secret_access_key")

        return boto.connect_s3(accessKeyId, secretAccessKey)

"""
Iterates over all the keys in the buckets "inbox" folder and returns an array
of the boto.s3.key.Key objects corresponding to files that need to be copied.  
"""
def getKeysInInbox(s3Cxn, yumRepoBucketName):
    log("Checking inbox...")
    inbox = []
    bucket = s3Cxn.get_bucket(yumRepoBucketName, validate=False)
    log("   --> obtained bucket")
    keys = bucket.list(prefix=options.inboxFolderName + "/")
    log("   --> obtained keys")
    for key in keys:
        if key.size > 0:
            inbox.append(key)
    return inbox

"""
Download the remote keys to the local destination folder, optionally removing a prefix from the key names
before creating the final local directory structure.
"""
def downloadKeys(keys, localDestination, removePrefixFromKeyName = None):
    log("Downloading keys to local staging area...")
    skippedKeys = []
    for key in keys:
        keyName = key.name
        if removePrefixFromKeyName != None:
            keyName = chompLeft(keyName, removePrefixFromKeyName)

        localFileName = localDestination + "/" + keyName
        localDir = os.path.dirname(localFileName)
        if not os.path.exists(localDir):
            os.makedirs(localDir)
        log("   --> %s" % (localFileName))
        try:
            key.get_contents_to_filename(localFileName)
        except S3ResponseError, e:
            log("      --> Error downloading file from s3 [%s] - skipping..." % key.name)
            skippedKeys.append(key)
    recordKeys(skippedKeys, 'skipped')
    return skippedKeys

"""
Delete the provided list of S3 keys, leaving around anything in skippedKeys
"""
def deleteKeys(keys, skippedKeys):
    # Now that everything has been successfully copied - delete the source keys
    for key in keys:
        if key not in skippedKeys:
            log("   --> deleting %s" % key.name)
            key.delete()

"""
Execute a shell command (i.e. createrepo)
"""
def execute(command):
    log("Executing: %s" % command)
    return subprocess.Popen(command, stdout=subprocess.PIPE).communicate()[0]


"""
Determine if there are more than the current instance of the application running at the current time.
"""
def isOnlyInstance():
    return os.system("(( $(ps -ef | grep python | grep '[" + __main__.__file__[0] + "]" + __main__.__file__[1:] + "' | wc -l) > 1 ))") != 0

"""
We never want to completely remove a package from our repo. So we will always keep at least X versions of any particular package.
If we have any more than X versions of a particular package, then we will remove any versions older than some age threshold.
"""
def pruneRepo(yumRepoBucketName):
    if options.perFolder:
        for subdir in os.listdir(options.localStagingArea + "/" + yumRepoBucketName):
            if os.path.isdir(options.localStagingArea + "/" + yumRepoBucketName + "/" + subdir):
                pruneRepoFolder(options.localStagingArea + "/" + yumRepoBucketName + "/" + subdir)
    else:
        pruneRepoFolder(options.localStagingArea + "/" + yumRepoBucketName)

def pruneRepoFolder(repoFolder):
    nowSec = time.time()
    ageThresholdInSec = options.pruneAgeDays * 24 * 60 * 60
    log("Finding the oldest versions of each individual package in " + repoFolder + "...")
    files = execute(["repomanage", "--keep=" + str(options.keepAtLeast), "--old", "--nocheck", repoFolder])
    log("Completed finding the oldest versions of each individual package")
    log("Checking each file to determine it's age...")
    for oldfile in files.split('\n'):
        if os.path.exists(oldfile):
            mtime = os.path.getmtime(oldfile)
            if (nowSec - mtime) > ageThresholdInSec:
                log("   --> pruning package from repo because it is so old: %s" % oldfile)
                os.remove(oldfile)  # TODO: Consider archiving these builds somewhere off to the side instead of just deleting them
    log("Completed pruning operation")

"""
Create the repo metadata for the folder(s) containing the RPMs
"""
def createRepo(yumRepoBucketName):
    if options.perFolder:
        for subdir in os.listdir(options.localStagingArea + "/" + yumRepoBucketName):
            if os.path.isdir(options.localStagingArea + "/" + yumRepoBucketName + "/" + subdir):
                if not os.path.exists(options.localCache + "/" + subdir):
                    os.makedirs(options.localCache + "/" + subdir)

                updateRepoMetadata(options.localStagingArea + "/" + yumRepoBucketName + "/" + subdir, options.localCache + "/" + subdir)
    else:
        updateRepoMetadata(options.localStagingArea + "/" + yumRepoBucketName, options.localCache)

def updateRepoMetadata(repoFolder, repoCacheDir):
    # Create (or update) the local repo
    log("Executing createrepo command for " + repoFolder + "...")
    log(execute(["/usr/bin/createrepo",
                 repoFolder,
                 "--simple-md-filenames",
                 "--skip-stat",
                 "--cachedir",
                 repoCacheDir]))
    log("Completed createrepo command")

"""
This is the main portion of the app that checks the inbox and coordinates getting the repo updated.
"""
def manageYumRepo():
    for yumRepoBucketName in options.yumRepoBucketNames:
        log("=================================================")
        log("== Working on bucket: %s" % yumRepoBucketName)
        log("=================================================")

        # Get a connection to S3 through one of a couple different methods
        s3Cxn = getS3Cxn()

        # Look in the inbox to see if there are any new files to add to the repo
        inboxKeys = getKeysInInbox(s3Cxn, yumRepoBucketName)
        recordKeys(inboxKeys, 'inbox', yumRepoBucketName)
        if len(inboxKeys) < 1:
            log("Nothing in the inbox - no work to do.")
            return

        # Since there is something in the inbox, sync the entire repo locally.  The first time, this could be pretty slow.
        # We need to be good about pruning our repos to keep them to a manageable size.
        if not os.path.exists(options.localStagingArea + "/" + yumRepoBucketName):
            os.makedirs(options.localStagingArea + "/" + yumRepoBucketName)
        log("Syncing remote repo to local staging area...")
        log(execute(["/usr/bin/aws",
                     "s3",
                     "sync",
                     "s3://" + yumRepoBucketName + "/" + yumRepoBucketName,
                     options.localStagingArea + "/" + yumRepoBucketName,
                     "--region",
                     options.yumRepoBucketRegion]))
        log("Completed sync operation")

        # Download the files from the inbox into the local repo staging area
        skippedKeys = downloadKeys(inboxKeys, options.localStagingArea + "/" + yumRepoBucketName, options.inboxFolderName + "/")

        # Prune old repo contents
        pruneRepo(yumRepoBucketName)

        # Create the repo metadata
        createRepo(yumRepoBucketName)

        # Sync the local copy of the repo back up to the s3 bucket.  Use the --delete option so that anything in the repo that was pruned will get removed from the bucket
        log("Syncing local staging area to remote repo...")
        log(execute(["/usr/bin/aws",
                     "s3",
                     "sync",
                     options.localStagingArea + "/" + yumRepoBucketName,
                     "s3://" + yumRepoBucketName + "/" + yumRepoBucketName,
                     "--delete",
                     "--region",
                     options.yumRepoBucketRegion]))
        log("Completed sync operation")

        # Now that we have successfully synced the results back up to s3, remove inbox items that were just processed
        log("Removing entries from inbox...")
        deleteKeys(inboxKeys, skippedKeys)
        log("Completed")

###############################################################
###############################################################
# Flask Routes

@app.route("/")
def index():
    return jsonify(history=url_for('.history', _external=True), jobs=url_for('.scheduledJobs', _external=True))

@app.route("/history")
def history():
    ret = []
    for lastRun in lastRuns:
        ret.append(lastRun)
    return jsonify(now=str(datetime.datetime.now()), previousRuns=ret)

@app.route("/scheduledJobs")
def scheduledJobs():
    ret = []
    for job in sched.get_jobs():
        ret.append({"runs": job.runs, "name": job.name, "next_run_time": job.next_run_time, "interval": str(job.trigger.interval)})
    return jsonify(scheduledJobs=ret)


###############################################################
###############################################################

lastRuns = deque()
(options, args) = parseArgs()

if options.version:
    print version
    sys.exit(0)

if not options.debug and not isOnlyInstance():
    # This application is already running! Aborting...
    sys.exit(1)

print "Logging output to %s" % options.logFile

# Schedule the yum manager job to run
sched = Scheduler()
log("Scheduling management job to run every %d seconds..." % options.timeInterval)
sched.add_interval_job(manageYumRepo, seconds=options.timeInterval, max_instances=1)
sched.start()

# Initialize flask
if options.debug:
    log("Running in debug mode")
app.run(host='0.0.0.0', debug=options.debug, use_reloader=False)

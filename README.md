YUM Repo Manager
========

This service manages a yum repo based in Amazon S3.  This service eliminates a lot of the race conditions that come
up when you have multiple sources updating the same repository's metadata files.  It solves that problem by
being the single entity to perform the writing and updating of the repository itself by watching an
"inbox" that others can place RPMs into.

All that it takes to publish a new RPM into the repository is to place the new RPM into the inbox. Any
sub-directories used in the inbox will be mimiced when the RPM is moved over to the repo itself.

For example, if you write to <s3bucket>/inbox/a/b/c/d.rpm, then the file will be put into <s3bucket>/repo/a/b/c/d.rpm
in order to copy the same organization scheme.

# Publishing a new RPM to the Repo
This is as simple as uploading your RPM file to the inbox in a sub-folder structure that models how/where you want it to
be arranged in the repo itself.  The inbox gets polled every 60 seconds (currently and changeable if needed) in order to
update the repo with new RPMs that have arrived since the last poll.

# Installing an RPM that Lives in the Repo
Your S3 bucket must be accessible by anyone wishing to install using your S3 repository. There are a few ways to do this,
1. [this plugin](https://github.com/seporaitis/yum-s3-iam) to use IAM instance profiles to read from
1. Make your S3 bucket a public 'website'
1. Ensure all of your instances IAM roles allow for access

After a new RPM has been installed to the repo, you may have to clear the local yum caches on the box before it will re-query
the repo and ask for a new list of the available packages.  The shotgun approach is:

    yum clean all
    yum makecache
    yum list available | grep -i <yourpackage>

# Monitoring
Should you have a Datadog account, the service posts a metric to Datadog every time that it runs.  The metric serves two 
purposes. First, it is a heartbeat to know that the service is still running.  If the service stops reporting it's metric, 
datadog will trigger an alert.

Second, the metric gives an idea of rate of new RPMs arriving in the repo that can be trended and graphed (or even alerted
if load gets really high in the future).

# View a list of scheduled jobs
View the scheduledJobs route

    curl -s "http://instanceip:5000/scheduledJobs" | python -mjson.tool

# View a history of the past runs
By default, the service keeps a log of the last 60 runs and what it found in the inbox (or didn't find) and the time at
which it ran.  To view that history:

    curl -s "http://instanceip:5000/history" | python -mjson.tool


import boto3
import datetime
import json
import logging
import socket
import ssl

sns = boto3.client('sns')
cloudfront = boto3.client('cloudfront')

logger = logging.getLogger()
logger.setLevel(logging.INFO)

class AlreadyExpired(Exception):
    pass

def ssl_expiry_datetime(hostname):
    ssl_date_fmt = r'%b %d %H:%M:%S %Y %Z'

    context = ssl.create_default_context()
    conn = context.wrap_socket(
        socket.socket(socket.AF_INET),
        server_hostname=hostname,
    )
    # 3 second timeout because Lambda has runtime limitations
    conn.settimeout(3.0)

    conn.connect((hostname, 443))
    ssl_info = conn.getpeercert()
    return datetime.datetime.strptime(ssl_info['notAfter'], ssl_date_fmt)

def ssl_remaining_days(hostname):
    expires = ssl_expiry_datetime(hostname)
    logger.debug(
        "SSL cert for %s expires in %s days",
        hostname, expires.isoformat()
    )
    return expires - datetime.datetime.utcnow()

def ssl_expires_in(hostname, buffer_days=14):
    """Check if `hostname` SSL cert expires is within `buffer_days`.

    Raises `AlreadyExpired` if the cert is past due
    """
    remaining = ssl_remaining_days(hostname)

    # if the cert expires in less than two weeks, we should reissue it
    if remaining < datetime.timedelta(days=0):
        # cert has already expired - uhoh!
        raise AlreadyExpired("Cert expired %s days ago" % remaining.days)
    elif remaining < datetime.timedelta(days=buffer_days):
        # expires sooner than the buffer
        return True
    else:
        # everything is fine
        return False


def check_domain(domain, buffer_days=14):
    try:
        if not ssl_expires_in(domain, buffer_days):
            logger.info("SSL certificate doesn't expire for a while - you're set!")
            return {"success": True, "cert_status": "OK", "domain": domain}
        else:
            logger.warning("SSL certificate expires soon")
            return {
                "success": True,
                "domain": domain,
                "cert_status": "WARNING",
                "message": "certificate is expiring soon",
            }
    except AlreadyExpired:
        logger.exception("Certificate is expired, get worried!")
        return {"success": True, "domain": domain, "cert_status": "EXPIRED"}
    except:
        import traceback
        logger.exception("Failed to get certificate info")
        return {
            "success": False,
            "domain": domain,
            "cert_status": "unknown",
            "message": traceback.format_exc()
        }
        return message

def lambda_handler(event, context):
    custom_ssl_methods = ['sni-only', 'vip']
    results = []
    for dist in cloudfront.list_distributions()['DistributionList']['Items']:
        if (dist['ViewerCertificate'] and
                dist['ViewerCertificate'].get('SSLSupportMethod') in custom_ssl_methods):
            # then this distribution uses custom SSL, so we should worry about expiration
            domain = dist['Aliases']['Items'][0]
            result = check_domain(domain, event.get('buffer_days', 14))
            results.append(result)
            logger.debug("Got result %s for domain %s" % (json.dumps(result), domain))
            if result['cert_status'] != 'OK' and event.get('topic', False):
                # If cert expires soon and we have a notification topic
                sns.publish(
                    TopicArn=event['topic'],
                    Message=json.dumps(result)
                )
    return results

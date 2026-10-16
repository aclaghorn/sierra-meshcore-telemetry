import * as cdk from 'aws-cdk-lib/core';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as targets from 'aws-cdk-lib/aws-route53-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import { Construct } from 'constructs';

const DOMAIN_NAME = 'telemetry.sierrame.sh';
const HOSTED_ZONE_ID = 'Z089631327MQ0PVCOFG5M';
const HOSTED_ZONE_NAME = 'sierrame.sh';

export class MeshcoreTelemetry extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const bucket = new s3.Bucket(this, 'PublicSiteBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const zone = route53.HostedZone.fromHostedZoneAttributes(this, 'HostedZone', {
      hostedZoneId: HOSTED_ZONE_ID,
      zoneName: HOSTED_ZONE_NAME,
    });

    // CloudFront requires the certificate to live in us-east-1 (this stack's region).
    const certificate = new acm.Certificate(this, 'SiteCertificate', {
      domainName: DOMAIN_NAME,
      validation: acm.CertificateValidation.fromDns(zone),
    });

    const distribution = new cloudfront.Distribution(this, 'PublicDistribution', {
      domainNames: [DOMAIN_NAME],
      certificate,
      defaultRootObject: 'index.html',
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        compress: true,
      },
      additionalBehaviors: {
        'data/*': {
          origin: origins.S3BucketOrigin.withOriginAccessControl(bucket),
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
          compress: true,
        },
      },
    });

    new route53.ARecord(this, 'SiteAliasRecord', {
      zone,
      recordName: DOMAIN_NAME,
      target: route53.RecordTarget.fromAlias(new targets.CloudFrontTarget(distribution)),
    });
    new route53.AaaaRecord(this, 'SiteAliasRecordIpv6', {
      zone,
      recordName: DOMAIN_NAME,
      target: route53.RecordTarget.fromAlias(new targets.CloudFrontTarget(distribution)),
    });

    new s3deploy.BucketDeployment(this, 'DeployPublicSite', {
      sources: [s3deploy.Source.asset('./site')],
      destinationBucket: bucket,
      distribution,
      distributionPaths: ['/', '/index.html', '/app.js', '/styles.css'],
      prune: false,
    });

    new cdk.CfnOutput(this, 'BucketName', {
      value: bucket.bucketName,
      description: 'Set publishing.bucket to this value.',
    });
    new cdk.CfnOutput(this, 'DistributionUrl', {
      value: `https://${distribution.distributionDomainName}`,
    });
    new cdk.CfnOutput(this, 'SiteUrl', {
      value: `https://${DOMAIN_NAME}`,
    });
  }
}

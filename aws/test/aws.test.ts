import * as cdk from 'aws-cdk-lib/core';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { MeshcoreTelemetry } from '../lib/meshcore-telemetry-stack';

const app = new cdk.App();
const stack = new MeshcoreTelemetry(app, 'TestStack', { env: { region: 'us-east-1' } });
const template = Template.fromStack(stack);

test('creates a private encrypted bucket', () => {
  template.hasResourceProperties('AWS::S3::Bucket', {
    BucketEncryption: {
      ServerSideEncryptionConfiguration: [
        { ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' } },
      ],
    },
    PublicAccessBlockConfiguration: {
      BlockPublicAcls: true,
      BlockPublicPolicy: true,
      IgnorePublicAcls: true,
      RestrictPublicBuckets: true,
    },
  });
});

test('serves the bucket through CloudFront OAC', () => {
  template.hasResourceProperties('AWS::CloudFront::Distribution', {
    DistributionConfig: Match.objectLike({
      DefaultRootObject: 'index.html',
      Enabled: true,
      PriceClass: 'PriceClass_100',
      Origins: Match.arrayWith([
        Match.objectLike({
          OriginAccessControlId: Match.anyValue(),
        }),
      ]),
    }),
  });
});

test('does not create IAM users', () => {
  template.resourceCountIs('AWS::IAM::User', 0);
});

test('serves the custom domain with a DNS-validated certificate', () => {
  template.hasResourceProperties('AWS::CertificateManager::Certificate', {
    DomainName: 'telemetry.sierrame.sh',
    ValidationMethod: 'DNS',
    DomainValidationOptions: [
      { DomainName: 'telemetry.sierrame.sh', HostedZoneId: 'Z089631327MQ0PVCOFG5M' },
    ],
  });
  template.hasResourceProperties('AWS::CloudFront::Distribution', {
    DistributionConfig: Match.objectLike({
      Aliases: ['telemetry.sierrame.sh'],
      ViewerCertificate: Match.objectLike({ SslSupportMethod: 'sni-only' }),
    }),
  });
  for (const type of ['A', 'AAAA']) {
    template.hasResourceProperties('AWS::Route53::RecordSet', {
      Name: 'telemetry.sierrame.sh.',
      Type: type,
      HostedZoneId: 'Z089631327MQ0PVCOFG5M',
      AliasTarget: Match.objectLike({ DNSName: Match.anyValue() }),
    });
  }
});

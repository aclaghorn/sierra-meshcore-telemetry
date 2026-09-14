import * as cdk from 'aws-cdk-lib/core';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { MeshcoreTelemetry } from '../lib/meshcore-telemetry-stack';

const app = new cdk.App();
const stack = new MeshcoreTelemetry(app, 'TestStack');
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

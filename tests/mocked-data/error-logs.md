
Error logs which we usually see-

2026-04-08T10:01:11Z ERROR [checkout-api] Startup failed: environment variable DB_HOST is required
2026-04-08T10:01:11Z ERROR [checkout-api] java.lang.IllegalStateException: Missing required config: DB_HOST
2026-04-08T10:01:12Z INFO  [checkout-api] Shutting down application

2026-04-08T10:02:03Z ERROR [checkout-api] Failed to connect to postgres.orders.svc.cluster.local:5432 - dial tcp 10.96.18.24:5432: connect: connection refused
2026-04-08T10:02:03Z WARN  [checkout-api] Retry attempt=1 backoff=5s
2026-04-08T10:02:08Z WARN  [checkout-api] Retry attempt=2 backoff=10s
2026-04-08T10:02:18Z ERROR [checkout-api] Database unavailable after 3 retries

2026-04-08T10:03:20Z ERROR [product-api] Failed to load secret: secret "catalog-db-secret" not found
2026-04-08T10:03:20Z ERROR [product-api] CreateContainerConfigError: unable to populate environment variables from secretRef
2026-04-08T10:03:20Z INFO  [product-api] Container startup aborted

2026-04-08T10:04:41Z ERROR [thumbnail-worker] fatal error: runtime: out of memory
2026-04-08T10:04:41Z WARN  [thumbnail-worker] processed_batch=428 memory_mb=512 limit_mb=512
2026-04-08T10:04:41Z INFO  [thumbnail-worker] exiting with code 137

2026-04-08T10:05:07Z ERROR [auth-service] User "system:serviceaccount:identity:auth-sa" cannot list resource "secrets" in API group "" in the namespace "identity"
2026-04-08T10:05:07Z ERROR [auth-service] Kubernetes API request failed status=403 reason=Forbidden
2026-04-08T10:05:08Z WARN  [auth-service] Falling back to cached credentials

2026-04-08T10:06:32Z ERROR [order-processor] failed to download config from s3://orders-bucket/config.json: AccessDenied
2026-04-08T10:06:32Z ERROR [order-processor] User: arn:aws:sts::123456789012:assumed-role/eks-node-role/i-0abc123 is not authorized to perform: s3:GetObject on resource: arn:aws:s3:::orders-bucket/config.json
2026-04-08T10:06:33Z WARN  [order-processor] IRSA token missing or invalid, falling back to node role

2026-04-08T10:07:15Z ERROR [order-processor] lookup redis.orders.svc.cluster.local on 172.20.0.10:53: no such host
2026-04-08T10:07:15Z ERROR [order-processor] failed to initialize redis client: name resolution failed
2026-04-08T10:07:16Z WARN  [order-processor] retrying redis connection in 5s

2026-04-08T10:08:01Z ERROR [web-frontend] upstream request failed: connect ECONNREFUSED 10.100.12.17:8080
2026-04-08T10:08:01Z ERROR [web-frontend] backend service unavailable
2026-04-08T10:08:02Z WARN  [web-frontend] serving fallback 503 page

2026-04-08T10:08:47Z ERROR [reporting-api] mount failed for volume "data": timed out waiting for the condition
2026-04-08T10:08:47Z ERROR [reporting-api] Unable to attach or mount volumes: unmounted volumes=[data], unattached volumes=[data tmp kube-api-access-x1z9k]
2026-04-08T10:08:48Z WARN  [reporting-api] PVC status is Pending

2026-04-08T10:09:22Z ERROR [checkout-api] Readiness probe failed: Get "http://10.0.55.12:8080/ready": dial tcp 10.0.55.12:8080: connect: connection refused
2026-04-08T10:09:22Z WARN  [checkout-api] Pod marked NotReady
2026-04-08T10:09:23Z INFO  [checkout-api] waiting for application warmup

2026-04-08T10:10:11Z ERROR [checkout-api] Liveness probe failed: HTTP probe failed with statuscode: 500
2026-04-08T10:10:11Z WARN  [checkout-api] kubelet will restart container
2026-04-08T10:10:12Z INFO  [checkout-api] received SIGTERM, shutting down

2026-04-08T10:11:36Z ERROR [billing-api] failed to bind to port 8080: address already in use
2026-04-08T10:11:36Z ERROR [billing-api] ContainerCannotRun: startup command failed
2026-04-08T10:11:37Z INFO  [billing-api] process exited status=1

2026-04-08T10:12:04Z ERROR [inventory-api] exec /app/server: no such file or directory
2026-04-08T10:12:04Z ERROR [inventory-api] ContainerCannotRun
2026-04-08T10:12:05Z INFO  [inventory-api] process exited status=127

2026-04-08T10:13:18Z ERROR [recommendation-api] panic: runtime error: invalid memory address or nil pointer dereference
2026-04-08T10:13:18Z ERROR [recommendation-api] stacktrace=main.loadModel(0x0)
2026-04-08T10:13:19Z INFO  [recommendation-api] process exited status=2

2026-04-08T10:14:41Z ERROR [media-api] failed to fetch object from S3: RequestError: send request failed caused by: Post "https://s3.amazonaws.com/": dial tcp: i/o timeout
2026-04-08T10:14:41Z WARN  [media-api] external dependency timeout service=s3
2026-04-08T10:14:42Z ERROR [media-api] request aborted due to dependency failure

2026-04-08T10:15:29Z ERROR [search-api] failed to connect to Elasticsearch at http://elasticsearch.logging.svc.cluster.local:9200: context deadline exceeded
2026-04-08T10:15:29Z WARN  [search-api] downstream service timeout dependency=elasticsearch
2026-04-08T10:15:30Z INFO  [search-api] circuit breaker opened

2026-04-08T10:16:55Z ERROR [gateway] upstream connect error or disconnect/reset before headers. reset reason: connection termination
2026-04-08T10:16:55Z WARN  [gateway] returning status=502
2026-04-08T10:16:56Z ERROR [gateway] ingress backend "payments/checkout-api-svc" has no active endpoints

2026-04-08T10:17:31Z ERROR [analytics-worker] HPA failed to get cpu utilization: unable to get metrics for resource cpu: no metrics returned from resource metrics API
2026-04-08T10:17:31Z WARN  [analytics-worker] autoscaling disabled due to missing metrics

2026-04-08T10:18:12Z ERROR [cluster-autoscaler] Failed to regenerate ASG cache: AccessDenied: User is not authorized to perform autoscaling:DescribeAutoScalingGroups
2026-04-08T10:18:12Z WARN  [cluster-autoscaler] cannot scale node group due to AWS API permission issue

2026-04-08T10:19:49Z ERROR [coredns] plugin/errors: 2 redis.orders.svc.cluster.local. A: read udp 10.0.22.10:49832->172.20.0.2:53: i/o timeout
2026-04-08T10:19:49Z WARN  [coredns] upstream DNS timeout
2026-04-08T10:19:50Z ERROR [coredns] health check failed

2026-04-08T10:20:26Z ERROR [kubelet] Error: ImagePullBackOff
2026-04-08T10:20:26Z ERROR [kubelet] Failed to pull image "123456789012.dkr.ecr.us-east-1.amazonaws.com/checkout-api:release-2026.04.08": rpc error: code = NotFound desc = failed to pull and unpack image
2026-04-08T10:20:27Z WARN  [kubelet] Back-off pulling image "123456789012.dkr.ecr.us-east-1.amazonaws.com/checkout-api:release-2026.04.08"

2026-04-08T10:21:15Z ERROR [scheduler] 0/6 nodes are available: 3 Insufficient cpu, 2 node(s) had taint {dedicated=batch: NoSchedule}, 1 node(s) didn't match Pod's node affinity
2026-04-08T10:21:15Z WARN  [scheduler] pod=reporting-api-688cf8d79f-vc45h remains Pending

2026-04-08T10:22:03Z ERROR [node-controller] Node ip-10-0-22-55.ec2.internal status is now: NodeNotReady
2026-04-08T10:22:03Z WARN  [node-controller] evicting pods from unhealthy node

2026-04-08T10:22:54Z ERROR [ingress-nginx] Service "edge/web-frontend-svc" does not have any active Endpoint
2026-04-08T10:22:54Z WARN  [ingress-nginx] backend unavailable host=app.example.com path=/

2026-04-08T10:23:41Z ERROR [external-secrets] failed calling webhook "validate.nginx.ingress.kubernetes.io": Post "https://ingress-nginx-controller-admission.ingress-nginx.svc:443/networking/v1/ingresses?timeout=10s": no endpoints available for service
2026-04-08T10:23:41Z WARN  [external-secrets] admission webhook unavailable

2026-04-08T10:24:33Z ERROR [payments-job] Job failed: BackoffLimitExceeded
2026-04-08T10:24:33Z ERROR [payments-job] pod payments-job-7x2mt terminated with exit code 1
2026-04-08T10:24:34Z WARN  [payments-job] retries exhausted

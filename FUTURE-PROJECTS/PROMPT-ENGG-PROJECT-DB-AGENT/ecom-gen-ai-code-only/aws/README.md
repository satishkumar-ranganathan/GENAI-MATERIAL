# Database Schema Management Application Deployment

This README provides instructions for deploying a Streamlit frontend and FastAPI backend application into two separate AWS ECS containers, using a free-tier RDS PostgreSQL instance and an Application Load Balancer (ALB) for the backend. The application manages database schemas, processes queries, and tracks audit logs for an e-commerce database. Configuration files (`config.json`, `table.txt`) are stored in S3, and database credentials are retrieved from AWS Secrets Manager.

## Prerequisites

- **AWS Account**: Access to ECS, RDS, EC2, ALB, S3, and Secrets Manager services.
- **Docker**: Installed locally for building container images.
- **AWS CLI**: Configured with credentials (`aws configure`).
- **OpenAI API Key**: Required for backend LLM functionality.
- **Application Code**: Streamlit frontend (`app.py`), FastAPI backend (`main.py`), and configuration files (`config.json`, `table.txt`).
- **Basic Knowledge**: Familiarity with AWS services, Docker, and PostgreSQL.

## Project Structure

```
project/
├── frontend/
│   ├── app.py                # Streamlit application
│   ├── Dockerfile            # Docker configuration for frontend
│   ├── requirements.txt      # Frontend dependencies
│   └── config/
│       ├── config.json       # Configuration file (local copy for testing)
│       └── table.txt         # Table context (local copy for testing)
├── backend/
│   ├── main.py               # FastAPI application
│   ├── Dockerfile            # Docker configuration for backend
│   ├── requirements.txt      # Backend dependencies
│   └── config/
│       ├── config.json       # Configuration file (local copy for testing)
│       └── table.txt         # Table context (local copy for testing)
```

## Setup Instructions

### 1. Prepare Application for Containerization

#### 1.1. Create Dockerfiles

**Frontend Dockerfile** (`frontend/Dockerfile`):
```dockerfile
FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
```

**Frontend Requirements** (`frontend/requirements.txt`):
```
streamlit==1.38.0
requests==2.31.0
```

**Backend Dockerfile** (`backend/Dockerfile`):
```dockerfile
FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8002
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
```

**Backend Requirements** (`backend/requirements.txt`):
```
fastapi==0.104.1
uvicorn==0.24.0.post1
psycopg2-binary==2.9.9
langchain-openai==0.2.0
pydantic==2.5.2
openai==1.3.7
boto3==1.34.0
```

#### 1.2. Update Configuration

- **Frontend**: Set `API_BASE_URL` to the ALB DNS (placeholder for now):
  ```python
  API_BASE_URL = "http://<alb-dns>:8002"
  ```
- **Backend**:
  - Loads `config.json` and `table.txt` from S3.
  - Retrieves database credentials from AWS Secrets Manager.
  - Example configuration in `main.py`:
    ```python
    S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "myapp-config-bucket")
    S3_CONFIG_KEY = os.getenv("S3_CONFIG_KEY", "config.json")
    S3_TABLE_KEY = os.getenv("S3_TABLE_KEY", "table.txt")
    SECRETS_ARN = os.getenv("DB_SECRETS_ARN")
    ```

### 2. Set Up AWS Resources

#### 2.1. Create RDS PostgreSQL Instance (Free Tier)

1. Navigate to **RDS** in AWS Console.
2. Create a **PostgreSQL** database:
   - Template: Free Tier
   - DB instance identifier: `ecommerce-db`
   - Master username: `admin`
   - Master password: (Save securely)
   - Instance type: `db.t3.micro`
   - Storage: 20 GB
   - VPC: Default or custom VPC
   - Security Group: Allow inbound PostgreSQL (port 5432) from ECS backend security group
   - Public access: No
3. Note the RDS endpoint (e.g., `ecommerce-db.xxxxx.rds.amazonaws.com:5432`).
4. Initialize database using a PostgreSQL client:
   ```sql
   CREATE SCHEMA ecommerce;
   CREATE TABLE ecommerce.schema_audit (
       id SERIAL PRIMARY KEY,
       table_name VARCHAR(255) NOT NULL,
       event_type VARCHAR(50) NOT NULL,
       changes JSONB NOT NULL,
       event_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
   );
   ```
   Populate with sample data if required.

#### 2.2. Create ECR Repositories

1. Navigate to **ECR** in AWS Console.
2. Create repositories: `frontend` and `backend`.
   ```bash
   aws ecr create-repository --repository-name frontend --region us-east-1
   aws ecr create-repository --repository-name backend --region us-east-1
   ```
3. Build and push Docker images:
   ```bash
   # Authenticate Docker to ECR
   aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account-id>.dkr.ecr.us-east-1.amazonaws.com

   # Frontend
   cd frontend
   docker build -t frontend .
   docker tag frontend:latest <account-id>.dkr.ecr.us-east-1.amazonaws.com/frontend:latest
   docker push <account-id>.dkr.ecr.us-east-1.amazonaws.com/frontend:latest

   # Backend
   cd backend
   docker build -t backend .
   docker tag backend:latest <account-id>.dkr.ecr.us-east-1.amazonaws.com/backend:latest
   docker push <account-id>.dkr.ecr.us-east-1.amazonaws.com/backend:latest
   ```
4. **Troubleshooting**:
   - If you encounter `The repository with name 'frontend' does not exist`:
     - Verify the repository exists:
       ```bash
       aws ecr describe-repositories --repository-names frontend --region us-east-1
       ```
     - Create the repository if missing:
       ```bash
       aws ecr create-repository --repository-name frontend --region us-east-1
       ```
     - Ensure your AWS CLI is configured with the correct account (`<account-id>`) and region (`us-east-1`).
     - Verify IAM permissions for ECR operations:
       ```json
       {
         "Version": "2012-10-17",
         "Statement": [
           {
             "Effect": "Allow",
             "Action": [
               "ecr:GetAuthorizationToken",
               "ecr:BatchCheckLayerAvailability",
               "ecr:GetDownloadUrlForLayer",
               "ecr:BatchGetImage",
               "ecr:PutImage",
               "ecr:InitiateLayerUpload",
               "ecr:UploadLayerPart",
               "ecr:CompleteLayerUpload",
               "ecr:CreateRepository",
               "ecr:DescribeRepositories"
             ],
             "Resource": "*"
           }
         ]
       }
       ```

#### 2.3. Set Up S3 Bucket

1. Create an S3 bucket (`myapp-config-bucket`).
2. Upload `config.json` and `table.txt` to the bucket.
3. Add S3 read permissions to the ECS task execution role:
   ```json
   {
     "Effect": "Allow",
     "Action": [
       "s3:GetObject"
     ],
     "Resource": "arn:aws:s3:::myapp-config-bucket/*"
   }
   ```

#### 2.4. Set Up Secrets Manager

1. Create a secret in Secrets Manager for RDS credentials:
   - Name: `ecommerce-db-credentials`
   - Secret value:
     ```json
     {
       "dbname": "olist_ecommerce",
       "username": "<rds-username>",
       "password": "<rds-password>",
       "host": "<rds-endpoint>",
       "port": "5432"
     }
     ```
2. Note the Secret ARN.
3. Add Secrets Manager permissions to the ECS task execution role:
   ```json
   {
     "Effect": "Allow",
     "Action": [
       "secretsmanager:GetSecretValue"
     ],
     "Resource": "<secret-arn>"
   }
   ```

### 3. Set Up ECS Cluster and Services

#### 3.1. Create ECS Cluster

1. Navigate to **ECS** in AWS Console.
2. Create cluster:
   - Name: `ecommerce-cluster`
   - Infrastructure: AWS Fargate
   - VPC: Same as RDS
   - Subnets: At least two subnets

#### 3.2. Create Task Definitions

**Frontend Task Definition**:
- Name: `frontend-task`
- Container:
  - Name: `frontend-container`
  - Image: `<account-id>.dkr.ecr.us-east-1.amazonaws.com/frontend:latest`
  - Port: 8501 (TCP)
  - CPU: 0.25 vCPU
  - Memory: 0.5 GB
- Environment Variables:
  - `API_BASE_URL`: `http://<alb-dns>:8002`
- Execution Role: Allow ECR and CloudWatch Logs access:
  ```json
  {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ],
        "Resource": "*"
      }
    ]
  }
  ```

**Backend Task Definition**:
- Name: `backend-task`
- Container:
  - Name: `backend-container`
  - Image: `<account-id>.dkr.ecr.us-east-1.amazonaws.com/backend:latest`
  - Port: 8002 (TCP)
  - CPU: 0.25 vCPU
  - Memory: 0.5 GB
- Environment Variables:
  - `OPENAI_API_KEY`: Your OpenAI API key
  - `S3_BUCKET_NAME`: `myapp-config-bucket`
  - `S3_CONFIG_KEY`: `config.json`
  - `S3_TABLE_KEY`: `table.txt`
  - `DB_SECRETS_ARN`: `<secret-arn>`
  - `FRONTEND_URL`: `http://<frontend-ip>:8501`
- Execution Role: Include permissions for ECR, CloudWatch, S3, and Secrets Manager:
  ```json
  {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "s3:GetObject",
          "secretsmanager:GetSecretValue"
        ],
        "Resource": [
          "*",
          "arn:aws:s3:::myapp-config-bucket/*",
          "<secret-arn>"
        ]
      }
    ]
  }
  ```

#### 3.3. Create ECS Services

**Frontend Service**:
- Name: `frontend-service`
- Task Definition: `frontend-task`
- Cluster: `ecommerce-cluster`
- Type: Fargate
- Tasks: 1
- Networking:
  - VPC: Same as RDS
  - Subnets: Public
  - Security Group: Allow HTTP (port 8501) from 0.0.0.0/0
  - Public IP: Yes
- Load Balancer: None

**Backend Service**:
- Name: `backend-service`
- Task Definition: `backend-task`
- Cluster: `ecommerce-cluster`
- Type: Fargate
- Tasks: 1
- Networking:
  - VPC: Same as RDS
  - Subnets: Private (or public if unavailable)
  - Security Group: Allow HTTP (port 8002) from ALB security group
  - Public IP: No
- Load Balancer: Configure ALB (see below)

#### 3.4. Create Application Load Balancer

1. Navigate to **EC2 > Load Balancers**.
2. Create ALB:
   - Name: `backend-alb`
   - Scheme: Internet-facing
   - VPC: Same as ECS/RDS
   - Subnets: Public
   - Listeners: HTTP (port 80)
   - Security Group: Allow HTTP (port 80) from 0.0.0.0/0
3. Create Target Group:
   - Name: `backend-targets`
   - Target type: IP
   - Protocol: HTTP
   - Port: 8002
   - Health check: `/health`
4. Register ECS backend service tasks with the target group.
5. Note ALB DNS (e.g., `backend-alb-xxxxx.us-east-1.elb.amazonaws.com`).
6. Update frontend `API_BASE_URL` to `http://<alb-dns>:8002` and redeploy.

#### 3.5. Configure Security Groups

- **RDS Security Group**: Allow PostgreSQL (port 5432) from backend security group.
- **Backend Security Group**: Allow HTTP (port 8002) from ALB security group.
- **Frontend Security Group**: Allow HTTP (port 8501) from 0.0.0.0/0.
- **ALB Security Group**: Allow HTTP (port 80) from 0.0.0.0/0, outbound to backend (port 8002).

### 4. Deploy and Test

1. Deploy ECS services:
   - Deploy `frontend-service` and `backend-service`.
   - Verify tasks are running in ECS console.
2. Access frontend:
   - Get frontend task public IP/DNS from ECS console.
   - Visit `http://<frontend-ip>:8501`.
3. Test backend:
   - Access `http://<alb-dns>/health`.
   - Verify frontend-backend communication via ALB.
4. Test RDS:
   - Use backend endpoints (e.g., `/schema/extract`) to confirm database connectivity.
   - Check CloudWatch Logs for errors.
5. Test application:
   - Use Streamlit to fetch schemas, process queries, and view audit logs.

### 5. Monitoring and Maintenance

#### 5.1. Monitoring

- **CloudWatch Logs**: View frontend/backend logs in ECS log groups.
- **ALB Metrics**: Monitor request count, latency, and errors.
- **RDS Metrics**: Track CPU, memory, and connections.
- **ECS Metrics**: Monitor task health and resource usage.

#### 5.2. Cleanup

To avoid costs:
- Delete ECS services and cluster.
- Terminate RDS instance.
- Delete ALB and target groups.
- Remove ECR images.
- Delete S3 bucket and Secrets Manager secret.
- Delete security groups and IAM roles.

### 6. Additional Notes

- **Free Tier Limits**:
  - RDS: 750 hours/month (`db.t3.micro`).
  - ECS Fargate: 400,000 GB-seconds/month.
  - ALB: 750 hours/month.
  - Monitor usage to avoid charges.
- **Security**:
  - Use AWS Secrets Manager for sensitive data (e.g., DB credentials, OpenAI API key).
  - Enable S3 bucket versioning and encryption.
  - Rotate Secrets Manager credentials regularly.
  - Update backend CORS to allow frontend DNS:
    ```python
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://<frontend-ip>:8501"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    ```
- **Scaling**:
  - Configure auto-scaling for backend service based on CPU/memory.
  - Adjust ALB target group scaling policies.
- **Domain**:
  - Attach a custom domain to ALB using Route 53 and ACM for SSL.
- **Troubleshooting**:
  - Check CloudWatch Logs for S3, Secrets Manager, or database errors.
  - Verify security group and VPC configurations.
  - Ensure Docker images are correctly built and pushed.
  - Confirm IAM role permissions for ECR, S3, and Secrets Manager.

### 7. AWS CLI Commands (Optional)

**Create ECR Repositories**:
```bash
aws ecr create-repository --repository-name frontend --region us-east-1
aws ecr create-repository --repository-name backend --region us-east-1
```

**Create ECS Cluster**:
```bash
aws ecs create-cluster --cluster-name ecommerce-cluster --region us-east-1
```

**Register Task Definitions**:
```bash
aws ecs register-task-definition --cli-input-json file://frontend-task.json --region us-east-1
aws ecs register-task-definition --cli-input-json file://backend-task.json --region us-east-1
```

**Create Services**:
```bash
aws ecs create-service --cluster ecommerce-cluster --service-name frontend-service --task-definition frontend-task --desired-count 1 --launch-type FARGATE --network-configuration "awsvpcConfiguration={subnets=[subnet-xxxx],securityGroups=[sg-xxxx],assignPublicIp=ENABLED}" --region us-east-1
aws ecs create-service --cluster ecommerce-cluster --service-name backend-service --task-definition backend-task --desired-count 1 --launch-type FARGATE --network-configuration "awsvpcConfiguration={subnets=[subnet-xxxx],securityGroups=[sg-xxxx],assignPublicIp=DISABLED}" --load-balancers "targetGroupArn=arn:aws:elasticloadbalancing:us-east-1:<account-id>:targetgroup/backend-targets/xxxx,containerName=backend-container,containerPort=8002" --region us-east-1
```

## Contact

For issues or questions, contact the project maintainer or open an issue in the repository.
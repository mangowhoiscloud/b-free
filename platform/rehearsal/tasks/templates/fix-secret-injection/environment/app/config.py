"""출발지(AWS) 방식: Secrets Manager를 직접 부릅니다. 목적지에서는 동작하지 않습니다."""
import boto3


def db_password() -> str:
    client = boto3.client("secretsmanager")
    return client.get_secret_value(SecretId="chat/db")["SecretString"]

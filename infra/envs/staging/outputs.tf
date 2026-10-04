output "aurora_endpoint" {
  value = module.aurora.endpoint
}

output "aurora_internal_name" {
  value = aws_route53_record.db.fqdn
}

output "aurora_master_secret_arn" {
  value = module.aurora.master_secret_arn
}

output "rag_store_urls" {
  value = module.rag.store_urls
}

output "rag_store_api_key_secret_arns" {
  value = module.rag.store_api_key_secret_arns
}

output "rag_house_read_only_key_secret_arn" {
  value = module.rag.house_read_only_key_secret_arn
}

output "database_secret_arns" {
  description = "Database -> the layers service's connection secret for it."
  value       = module.databases.secret_arns
}

output "provision_databases" {
  description = "Run after an apply that adds an agency."
  value       = "aws ssm send-command --region ${var.region} --document-name ${aws_ssm_document.provision_databases.name} --targets Key=InstanceIds,Values=${module.admin.instance_id}"
}

output "buckets" {
  value = module.storage.bucket_names
}

output "admin_instance_id" {
  value = module.admin.instance_id
}

output "data_clients_security_group_id" {
  description = "Stage 2 services join this group to reach Aurora and Qdrant."
  value       = aws_security_group.data_clients.id
}

output "tunnel_postgres" {
  description = "Run locally, then connect to localhost:5432."
  value       = "aws ssm start-session --region ${var.region} --target ${module.admin.instance_id} --document-name AWS-StartPortForwardingSessionToRemoteHost --parameters host=${module.aurora.endpoint},portNumber=5432,localPortNumber=5432"
}

output "tunnel_rag_house" {
  description = "Run locally, then open http://localhost:6333/dashboard. For an agency store use its port (6340 + slot)."
  value       = "aws ssm start-session --region ${var.region} --target ${module.admin.instance_id} --document-name AWS-StartPortForwardingSessionToRemoteHost --parameters host=${module.rag.private_ip},portNumber=6333,localPortNumber=6333"
}

output "model_invoke_policy_arn" {
  description = "Attach to any role that calls Claude on Bedrock (NAPKIN_MODEL_API=bedrock)."
  value       = aws_iam_policy.model_invoke.arn
}

output "cognito_user_pool_id" {
  description = "For scripts/create-users.sh"
  value       = module.identity.user_pool_id
}

output "cognito_client_id" {
  description = "NAPKIN_COGNITO_CLIENT_ID for napkin-web"
  value       = module.identity.client_id
}

output "session_secret_arn" {
  description = "NAPKIN_SESSION_SECRET for napkin-web (Secrets Manager)"
  value       = module.identity.session_secret_arn
}

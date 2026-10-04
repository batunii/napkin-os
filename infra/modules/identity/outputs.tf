output "user_pool_id" {
  description = "For scripts/create-users.sh"
  value       = aws_cognito_user_pool.studio.id
}

output "client_id" {
  description = "NAPKIN_COGNITO_CLIENT_ID for napkin-web"
  value       = aws_cognito_user_pool_client.web.id
}

output "session_secret_arn" {
  description = "NAPKIN_SESSION_SECRET for napkin-web, from Secrets Manager"
  value       = aws_secretsmanager_secret.session.arn
}

output "secret_arns" {
  description = "Database name -> its app connection secret."
  value       = { for k, s in aws_secretsmanager_secret.app : k => s.arn }
}

output "list_parameter_name" {
  value = aws_ssm_parameter.databases.name
}

output "list_parameter_arn" {
  value = aws_ssm_parameter.databases.arn
}

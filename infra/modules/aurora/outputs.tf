output "endpoint" {
  value = aws_rds_cluster.this.endpoint
}

output "port" {
  value = aws_rds_cluster.this.port
}

output "database_name" {
  value = aws_rds_cluster.this.database_name
}

output "cluster_resource_id" {
  description = "For IAM database auth policies (rds-db:connect)."
  value       = aws_rds_cluster.this.cluster_resource_id
}

output "master_secret_arn" {
  value = aws_rds_cluster.this.master_user_secret[0].secret_arn
}

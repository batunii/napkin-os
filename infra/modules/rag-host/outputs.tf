output "instance_id" {
  value = aws_instance.rag.id
}

output "private_ip" {
  value = aws_instance.rag.private_ip
}

output "store_urls" {
  description = "Store name -> URL. Retrieval routes by the caller's agency."
  value       = { for k in keys(local.stores) : k => "http://${aws_route53_record.store[k].fqdn}:${local.port[k]}" }
}

output "store_api_key_secret_arns" {
  description = "Store name -> its read-write key. Retrieval gets the agency ones only."
  value       = { for k, s in aws_secretsmanager_secret.api_key : k => s.arn }
}

output "house_read_only_key_secret_arn" {
  description = "The only house key retrieval is given."
  value       = aws_secretsmanager_secret.house_read_only.arn
}

output "store_secret_arns" {
  description = "Every key, for the admin host (ingestion writes the house store)."
  value = concat(
    [for s in aws_secretsmanager_secret.api_key : s.arn],
    [aws_secretsmanager_secret.house_read_only.arn],
  )
}

output "vpc_id" {
  value = aws_vpc.this.id
}

output "vpc_cidr" {
  value = aws_vpc.this.cidr_block
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "internal_zone_id" {
  value = aws_route53_zone.internal.zone_id
}

output "internal_domain" {
  value = aws_route53_zone.internal.name
}

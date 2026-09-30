# terraform test, against mocked providers: no AWS account, no cost.
#   cd infra/modules/rag-host && terraform init && terraform test

mock_provider "aws" {
  mock_data "aws_subnet" {
    defaults = { availability_zone = "eu-west-1a" }
  }
}
mock_provider "random" {}

variables {
  name                     = "napkin-test"
  region                   = "eu-west-1"
  vpc_id                   = "vpc-1"
  subnet_id                = "subnet-1"
  client_security_group_id = "sg-1"
  kms_key_arn              = "arn:aws:kms:eu-west-1:111111111111:key/data"
  internal_zone_id         = "Z1"
  internal_domain          = "napkin.internal"
  agencies = {
    acme   = { slot = 1 }
    globex = { slot = 7, volume_gb = 10, own_kms_key = false }
    old    = { slot = 3, state = "retired" }
  }
}

run "stores_get_fixed_ports_and_devices" {
  command = plan

  assert {
    condition     = local.port == { house = 6333, acme = 6341, globex = 6347, old = 6343 }
    error_message = "ports must be 6333 for house and 6340 + slot for agencies"
  }
  assert {
    condition     = local.device == { house = "/dev/sdf", acme = "/dev/sdg", globex = "/dev/sdm", old = "/dev/sdi" }
    error_message = "devices must be /dev/sd[f + slot]"
  }
  assert {
    condition     = keys(aws_kms_key.agency) == ["acme", "old"]
    error_message = "only agencies with own_kms_key get a key; house never does"
  }
  assert {
    condition     = aws_ebs_volume.store["globex"].size == 10 && aws_ebs_volume.store["acme"].size == 5
    error_message = "volume sizes must follow the agency entry"
  }
  assert {
    condition     = length(aws_secretsmanager_secret.api_key) == 4
    error_message = "every store, house included, has its own key"
  }
}

run "house_alone_by_default" {
  command = plan
  variables {
    agencies = {}
  }
  assert {
    condition     = keys(aws_ebs_volume.store) == ["house"]
    error_message = "with no agencies there is exactly one store"
  }
}

run "duplicate_slot_is_refused" {
  command = plan
  variables {
    agencies = { a = { slot = 2 }, b = { slot = 2 } }
  }
  expect_failures = [var.agencies]
}

run "house_is_a_reserved_name" {
  command = plan
  variables {
    agencies = { house = { slot = 1 } }
  }
  expect_failures = [var.agencies]
}

run "slot_out_of_range_is_refused" {
  command = plan
  variables {
    agencies = { acme = { slot = 21 } }
  }
  expect_failures = [var.agencies]
}

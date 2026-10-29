from django.db import models


class Station(models.Model):
    opis_id = models.IntegerField(unique=True)
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=200)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    price = models.FloatField()          # $ per gallon (exact observed lowest price from CSV)
    lat = models.FloatField()            # city-centre centroid fallback (CSV lacks GPS coords)
    lon = models.FloatField()
    rack_id = models.CharField(max_length=20, blank=True, default="")
    raw_record_count = models.PositiveIntegerField(default=1)
    is_exact_coord = models.BooleanField(default=False)
    coord_source = models.CharField(max_length=50, default="city_centre_fallback")

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state})"

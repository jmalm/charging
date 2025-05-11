from datetime import datetime, timedelta, timezone
import math
import random
import unittest
import yaml

from scheduling import extrapolate_prices, create_schedule, NotEnoughTimeException, calculate_eta, get_prices


class SchedulerTests(unittest.TestCase):
    def test__get_prices__incomplete_last_period(self):
        # Arrange
        start = datetime(2025, 1, 1)
        period = timedelta(seconds=1)
        end = start + 2 * period
        prices = list(_build_prices(start, end, period))

        # Act
        available_prices = get_prices(prices, start, start + 1.5 * period)

        # Assert
        self.assertEqual(2, len(available_prices), 'Should be 2 periods')
        self.assertEqual(start, available_prices[0]['start'], 'First period start')
        self.assertEqual(start + period, available_prices[0]['end'], 'First period end')
        self.assertEqual(start + period, available_prices[1]['start'], 'Second period start')
        self.assertEqual(start + 1.5 * period, available_prices[1]['end'], 'Second period end')

    def test__get_prices__incomplete_first_period(self):
        # Arrange
        start = datetime(2025, 1, 1)
        period = timedelta(seconds=1)
        end = start + 2 * period
        prices = list(_build_prices(start, end, period))

        # Act
        available_prices = get_prices(prices, start + 0.5 * period, end)

        # Assert
        self.assertEqual(2, len(available_prices), 'Should be 2 periods')
        self.assertEqual(start + 0.5 * period, available_prices[0]['start'], 'First period start')
        self.assertEqual(start + period, available_prices[0]['end'], 'First period end')
        self.assertEqual(start + period, available_prices[1]['start'], 'Second period start')
        self.assertEqual(start + 2 * period, available_prices[1]['end'], 'Second period end')


    def test__extrapolate_prices__one_missing_day(self):
        self._test__extrapolate_prices(
            start=datetime(2023, 1, 1, 0, 0, 0),
            existing = timedelta(days=1),
            period = timedelta(minutes=15),
            extension = timedelta(days=1)
        )

    def test__extrapolate_prices__three_missing_days(self):
        self._test__extrapolate_prices(
            start=datetime(2023, 1, 1, 0, 0, 0),
            existing = timedelta(days=1),
            period = timedelta(minutes=15),
            extension = timedelta(days=3)
        )

    def test__extrapolate_prices__two_days_exist(self):
        self._test__extrapolate_prices(
            start=datetime(2023, 1, 1, 0, 0, 0),
            existing = timedelta(days=2),
            period = timedelta(minutes=15),
            extension = timedelta(days=1)
        )

    def test__extrapolate_prices__short_extension(self):
        period = timedelta(minutes=15)
        self._test__extrapolate_prices(
            start=datetime(2023, 1, 1, 0, 0, 0),
            existing = timedelta(days=1),
            period = period,
            extension = period * 3
        )

    def test__extrapolate_prices__uneven_extension(self):
        self._test__extrapolate_prices(
            start=datetime(2023, 1, 1, 0, 0, 0),
            existing = timedelta(days=1),
            period = timedelta(minutes=15),
            extension = timedelta(minutes=73)
        )

    def _test__extrapolate_prices(self, start: datetime, existing: timedelta, period: timedelta, extension: timedelta):
        # Arrange
        end = start + existing
        prices = list(_build_prices(start, end, period))
        expected_num_periods = len(prices) + math.ceil(extension / period)
        extension_end = end + extension

        # Act
        filled = extrapolate_prices(prices, extension_end)

        # Assert
        self.assertEqual(start, filled[0]['start'], 'Expected the first period to be the same.')
        self.assertEqual(extension_end, filled[-1]['end'], f'Expected the last period to end at {extension_end}.')
        self.assertEqual(expected_num_periods, len(filled), f'Expected umber of periods to be {expected_num_periods}.')

        start_diff = [filled[i + 1]['start'] - filled[i]['start'] for i in range(0, len(filled) - 1)]
        end_diff = [filled[i + 1]['end'] - filled[i]['end'] for i in range(0, len(filled) - 1)]
        self.assertSequenceEqual([period] * (len(filled) - 1), start_diff, f'All periods should start {period} after the previous')
        self.assertSequenceEqual([period] * (len(filled) - 2), end_diff[:-1], f'All periods (except possibly the last) should end {period} after the previous')
        self.assertEqual(period, filled[0]['end'] - filled[0]['start'], f'The filled periods should be {period}')

    def test__extrapolate_prices__end_inside_available_periods(self):
        # Arrange
        start = datetime(2023, 1, 1, 0, 0, 0)
        existing = timedelta(days=1)
        period = timedelta(minutes=15)
        extension = timedelta(minutes=-73)
        end = start + existing
        prices = list(_build_prices(start, end, period))
        extension_end = end + extension

        # Act
        filled = extrapolate_prices(prices, extension_end)

        # Assert
        self.assertEqual(end, filled[-1]['end'], 'Expected the last period to end at the end of the last period.')

    def test_create_schedule(self):
        # Arrange
        start = datetime(2025, 1, 1)
        period = timedelta(minutes=15)
        available_periods = [
            {'start': start,              'end': start + period,      'value': 1},
            {'start': start + period,     'end': start + period * 2,  'value': 2},
            {'start': start + period * 2, 'end': start + period * 3,  'value': 1},
            {'start': start + period * 3, 'end': start + period * 4,  'value': 2},
            {'start': start + period * 4, 'end': start + period * 5,  'value': 3},
            {'start': start + period * 5, 'end': start + period * 6,  'value': 1},
            {'start': start + period * 6, 'end': start + period * 7,  'value': 2},
            {'start': start + period * 7, 'end': start + period * 8,  'value': 3},
            {'start': start + period * 8, 'end': start + period * 9,  'value': 4},
            {'start': start + period * 9, 'end': start + period * 10, 'value': 1},
        ]

        # Act
        schedule = create_schedule(available_periods, timedelta(hours=1.6))

        # Assert
        self.assertSequenceEqual([
            {'start': start, 'end': start + period * 4},
            {'start': start + period * 5, 'end': start + period * 7},
            {'start': start + period * 9, 'end': start + period * 10},
        ], schedule, 'schedule should be as expected')

    def test__create_schedule__no_available_periods(self):
        # Arrange
        available_periods = []

        # Act & Assert
        self.assertRaises(NotEnoughTimeException, create_schedule, available_periods, 1.6)

    def test__create_schedule__not_enough_time(self):
        # Arrange
        start = datetime(2025, 1, 1)
        period = timedelta(minutes=15)
        available_periods = [{'start': start, 'end': start + period, 'value': 1}]

        # Act & Assert
        self.assertRaises(NotEnoughTimeException, create_schedule, available_periods, timedelta(hours=1.6))

    def test__create_schedule__advanced(self):
        # Arrange
        with open('prices-2025-04-14.yaml', 'r') as f:
            data = yaml.load(f, Loader=yaml.FullLoader)
        available_periods = _parse_prices(data['Raw today'] + data['Raw tomorrow'])

        # Act
        schedule = create_schedule(available_periods, timedelta(hours=5.6))

        # Assert
        total_scheduled_time = sum([period['end'] - period['start'] for period in schedule], timedelta())
        self.assertEqual(timedelta(hours=6), total_scheduled_time, 'Total scheduled time should be 6 hours')
        self.assertEqual(2, len(schedule), 'Should be 2 periods')
        self.assertEqual(datetime(2025, 4, 14, 1, tzinfo=timezone(timedelta(hours=2))), schedule[0]['start'], 'First period start')
        self.assertEqual(datetime(2025, 4, 14, 5, tzinfo=timezone(timedelta(hours=2))), schedule[0]['end'], 'First period end')
        self.assertEqual(datetime(2025, 4, 15, 12, tzinfo=timezone(timedelta(hours=2))), schedule[1]['start'], 'Second period start')
        self.assertEqual(datetime(2025, 4, 15, 14, tzinfo=timezone(timedelta(hours=2))), schedule[1]['end'], 'Second period end')

    def test__create_schedule__incomplete_last_period(self):
        # Arrange
        start = datetime(2025, 1, 1)
        period = timedelta(seconds=1)
        available_periods = [
            {'start': start,              'end': start + period,       'value': 2},
            {'start': start + period,     'end': start + period * 2,   'value': 3},
            {'start': start + period * 2, 'end': start + period * 3,   'value': 4},
            {'start': start + period * 3, 'end': start + period * 3.1, 'value': 1},
        ]
        needed_time = period * 1.6

        # Act
        schedule = create_schedule(available_periods, needed_time)

        # Assert
        self.assertLessEqual(schedule[0]['end'], available_periods[-1]['end'], 'Charging should end on or before the last period ends')
        total_scheduled_time = sum([period['end'] - period['start'] for period in schedule], timedelta())
        self.assertGreaterEqual(total_scheduled_time, needed_time, 'Total scheduled time is less than needed time')
        self.assertEqual(2, len(schedule), 'Should be 2 periods')
        self.assertEqual(start, schedule[0]['start'], 'First period start')
        self.assertEqual(start + period * 2, schedule[0]['end'], 'First period end')
        # (The third period should be skipped.)
        self.assertEqual(start + period * 3, schedule[1]['start'], 'Second period start')
        self.assertEqual(start + period * 3.1, schedule[1]['end'], 'Second period end')

    def test__calculate_eta__no_schedule(self):
        # Arrange
        # schedule = [dict(start=datetime(2025, 1, 1, 0, 0), end=datetime(2025, 1, 1, 0, 15))]
        schedule = None
        start = datetime(2025, 1, 1, 0, 0)
        time_to_charge = timedelta(hours=3.5)
        expected_eta = start + time_to_charge

        # Act
        eta = calculate_eta(start, time_to_charge, schedule)

        # Assert
        self.assertEqual(expected_eta, eta, f"ETA is not the expected")

    def test__calculate_eta__empty_schedule(self):
        # Arrange
        # schedule = [dict(start=datetime(2025, 1, 1, 0, 0), end=datetime(2025, 1, 1, 0, 15))]
        schedule = []
        start = datetime(2025, 1, 1, 0, 0)
        time_to_charge = timedelta(hours=3.5)
        expected_eta = start + time_to_charge

        # Act
        eta = calculate_eta(start, time_to_charge, schedule)

        # Assert
        self.assertEqual(expected_eta, eta, f"ETA is not the expected")

    def test__calculate_eta__too_short_schedule(self):
        # Arrange
        schedule = [
            dict(start=datetime(2025, 1, 1, 5, 0),
                 end=datetime(2025, 1, 1, 6, 0))
        ]
        start = datetime(2025, 1, 1, 0, 0)
        time_to_charge = timedelta(hours=3.5)
        expected_eta = datetime(2025, 1, 1, 8, 30)

        # Act
        eta = calculate_eta(start, time_to_charge, schedule)

        # Assert
        self.assertEqual(expected_eta, eta, f"ETA is not the expected")

    def test__calculate_eta__too_long_schedule(self):
        # Arrange
        schedule = [
            dict(start=datetime(2025, 1, 1, 5, 0),
                 end=datetime(2025, 1, 1, 6, 0)),
            dict(start=datetime(2025, 1, 1, 8, 0),
                 end=datetime(2025, 1, 1, 11, 0))
        ]
        start = datetime(2025, 1, 1, 0, 0)
        time_to_charge = timedelta(hours=3.5)
        expected_eta = datetime(2025, 1, 1, 10, 30)

        # Act
        eta = calculate_eta(start, time_to_charge, schedule)

        # Assert
        self.assertEqual(expected_eta, eta, f"ETA is not the expected")

    def test__calculate_eta__too_early_and_too_long_schedule(self):
        # Arrange
        schedule = [
            dict(start=datetime(2025, 1, 1, 0, 0),
                 end=datetime(2025, 1, 1, 6, 0))
        ]
        start = datetime(2025, 1, 1, 1, 0)
        time_to_charge = timedelta(hours=3.5)
        expected_eta = datetime(2025, 1, 1, 4, 30)

        # Act
        eta = calculate_eta(start, time_to_charge, schedule)

        # Assert
        self.assertEqual(expected_eta, eta, f"ETA is not the expected")

def _build_prices(start: datetime, end: datetime, period: timedelta):
    while start < end:
        yield {'start': start, 'end': start + period, 'value': random.uniform(0, 1)}
        start += period


def _parse_prices(periods):
    return [{
        'start': datetime.fromisoformat(period['start']),
        'end': datetime.fromisoformat(period['end']),
        'value': period['value']
    } for period in periods]

if __name__ == '__main__':
    unittest.main()

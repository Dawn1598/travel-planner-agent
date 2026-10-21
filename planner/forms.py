from django import forms


class TripInputForm(forms.Form):
    start_location = forms.CharField(label='开始地点', max_length=200)
    start_date = forms.DateField(label='开始日期', input_formats=['%Y-%m-%d'])
    end_date = forms.DateField(label='结束日期', input_formats=['%Y-%m-%d'])
    requirements = forms.CharField(label='用户要求', max_length=6000, required=False)

    def clean(self):
        data = super().clean()
        if data.get('start_date') and data.get('end_date') and data['end_date'] < data['start_date']:
            self.add_error('end_date', '结束日期不能早于开始日期')
        return data

    def as_trip(self):
        return {key: value.isoformat() if key.endswith('_date') else value
                for key, value in self.cleaned_data.items()}
